"""
Integration tests against a real Elasticsearch instance.

These tests are excluded from the default pytest run (see pytest.ini). Run them with:

    poetry run pytest -m integration

Elasticsearch host/port are taken from the regular configuration
(env vars PY_IMAGE_DEDUP_ELASTICSEARCH_HOST / PY_IMAGE_DEDUP_ELASTICSEARCH_PORT or tests/py_image_dedup.yaml).
A separate index (PY_IMAGE_DEDUP_TEST_ELASTICSEARCH_INDEX, default "py-image-dedup-integration-test")
is used and deleted before and after the test run.
"""
import os
from pathlib import Path

import pytest
import requests

from py_image_dedup.config import DeduplicatorConfig
from py_image_dedup.library import ActionEnum

pytestmark = pytest.mark.integration

TEST_INDEX = os.environ.get("PY_IMAGE_DEDUP_TEST_ELASTICSEARCH_INDEX", "py-image-dedup-integration-test")

# each folder in tests/images/ contains one original photo plus
# an edited, a grayscale and a (telegram) compressed copy of it
IMAGE_GROUPS = {
    "bottles": "IMG_20190903_193151",
    "building": "IMG_20190903_193508",
    "clouds": "IMG_20190903_193537",
}


def _es_url(config: DeduplicatorConfig) -> str:
    return f"http://{config.ELASTICSEARCH_HOST.value}:{config.ELASTICSEARCH_PORT.value}"


def _config() -> DeduplicatorConfig:
    """
    Note: DeduplicatorConfig() reloads all values from its sources on every call,
    so the overrides have to be applied again every time.
    """
    config = DeduplicatorConfig()
    assert config.DRY_RUN.value is True, "integration tests must never touch files"
    config.ELASTICSEARCH_INDEX.value = TEST_INDEX
    # file modification times depend on how the repository was checked out
    config.MAX_FILE_MODIFICATION_TIME_DELTA.value = None
    return config


@pytest.fixture(scope="module")
def deduplicator():
    config = _config()

    es_url = _es_url(config)
    # ElasticSearchStoreBackend retries forever, so fail fast if Elasticsearch is not reachable
    try:
        requests.get(es_url, timeout=5).raise_for_status()
    except requests.RequestException as e:
        pytest.fail(f"Elasticsearch is not reachable at {es_url}: {e}")

    requests.delete(f"{es_url}/{TEST_INDEX}", timeout=10)

    from py_image_dedup.library.deduplicator import ImageMatchDeduplicator
    under_test = ImageMatchDeduplicator(interactive=False)
    yield under_test

    requests.delete(f"{es_url}/{TEST_INDEX}", timeout=10)


def _refresh_index(config: DeduplicatorConfig):
    """
    Make newly added documents visible to searches immediately
    """
    requests.post(f"{_es_url(config)}/{TEST_INDEX}/_refresh", timeout=10).raise_for_status()


def test_analyse_and_find_duplicates(deduplicator):
    config = _config()
    image_files = sorted(Path("images").rglob("*.jpg"))
    assert len(image_files) == 12

    deduplicator.analyse_all()
    _refresh_index(config)

    count, _ = deduplicator._persistence.get_all()
    assert count == len(image_files)

    result = deduplicator.deduplicate_all(skip_analyze_phase=True)

    # dry run: nothing has been touched on disk
    for image_file in image_files:
        assert image_file.exists()

    actions = {Path(path).as_posix(): action for path, action in result.item_actions.items()}
    for folder, base_name in IMAGE_GROUPS.items():
        group_actions = {path: action for path, action in actions.items() if f"/{folder}/" in f"/{path}"}
        kept = [path for path, action in group_actions.items() if action == ActionEnum.NONE]
        deleted = [path for path, action in group_actions.items() if action == ActionEnum.DELETE]

        # exactly one file per group is kept, at least one copy is detected as a duplicate
        assert len(kept) == 1, f"{folder}: {group_actions}"
        assert len(deleted) >= 1, f"{folder}: {group_actions}"
        # the compressed copy is a near-identical duplicate of the original
        group_files = {Path(path).name for path in kept + deleted}
        assert f"{base_name}.jpg" in group_files, f"{folder}: {group_actions}"
        assert f"{base_name}-telegram-compression.jpg" in group_files, f"{folder}: {group_actions}"

    # duplicates are never found across different photos
    assert result.get_duplicate_count() == len(IMAGE_GROUPS)


def test_analysing_again_does_not_add_duplicate_entries(deduplicator):
    config = _config()
    image_files = sorted(Path("images").rglob("*.jpg"))

    deduplicator.analyse_all()
    _refresh_index(config)
    deduplicator.analyse_all()
    _refresh_index(config)

    count, entries = deduplicator._persistence.get_all()
    assert count == len(image_files)
    stored_paths = sorted(entry["path"] for entry in entries)
    assert stored_paths == sorted(str(image_file) for image_file in image_files)


def test_get_and_remove_by_path(deduplicator):
    config = _config()
    persistence = deduplicator._persistence
    image_file = str(next(Path("images").rglob("*.jpg")))

    entry = persistence.get(image_file)
    assert entry is not None
    assert entry["path"] == image_file

    persistence.remove(image_file)
    _refresh_index(config)
    assert persistence.get(image_file) is None

    persistence.add(image_file)
    _refresh_index(config)
    assert persistence.get(image_file) is not None


def test_cleanup_removes_entries_of_missing_files(deduplicator):
    config = _config()
    persistence = deduplicator._persistence
    image_files = sorted(Path("images").rglob("*.jpg"))
    root_directories = config.SOURCE_DIRECTORIES.value
    missing_file = str(root_directories[0] / "does-not-exist.jpg")

    # store an entry for a file that does not exist (anymore), using the image data of an existing one
    existing_file = str(image_files[0])
    image_data = persistence._create_metadata_dict(existing_file)
    image_data["path"] = missing_file
    persistence._store.add_image(missing_file, img=existing_file, metadata=image_data)
    _refresh_index(config)
    assert persistence.get(missing_file) is not None

    deduplicator.cleanup_database(root_directories)
    _refresh_index(config)

    count, entries = persistence.get_all()
    assert missing_file not in [entry["path"] for entry in entries]

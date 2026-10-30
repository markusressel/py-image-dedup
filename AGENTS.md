# AGENTS.md

Guidance for AI coding agents (and humans) working on **py-image-dedup**.

## Project overview

py-image-dedup finds and removes/moves near-duplicate images in a photo library.
It computes a perceptual signature per image (via the `image-match` library), stores it in
Elasticsearch, queries Elasticsearch for similar signatures and then decides — based on
configurable prioritization rules — which copy to keep.

It runs either as a one-shot CLI (`analyse`, `deduplicate`) or as a long-running `daemon`
that watches the source directories, exposes Prometheus metrics and serves a web UI
(FastAPI JSON API + single page app, internal use, no authentication).

- Language: Python `^3.14` (see `pyproject.toml`)
- Packaging/deps: Poetry 2.x (`poetry.lock` is committed)
- Runtime dependency: Elasticsearch 7.x (see `docker-compose.yml`)
- License: AGPLv3+

## Setup & commands

```shell
poetry install                     # install incl. dev deps (pytest)
poetry run pytest                  # run unit tests (no Elasticsearch needed, < 1s)
poetry run py-image-dedup --help   # CLI entrypoint (py_image_dedup.cli:cli)
docker build . -t py-image-dedup:test   # same build CI runs on every PR
docker compose up                  # Elasticsearch + daemon, for manual end-to-end testing
```

- Always run `poetry run pytest` after code changes.
- After changing dependencies or the `Dockerfile`, also run the Docker build: CI only builds the image,
  and the build is the first place where missing wheels for the current Python version show up.
- Update dependencies with `poetry update --lock` (or `poetry add <pkg>@<constraint>`), never by editing `poetry.lock` by hand.

## Repository layout

```
py_image_dedup/
  cli.py                       # click CLI: analyse, deduplicate, daemon
  config.py                    # DeduplicatorConfig (container-app-conf): all config entries + defaults
  stats.py                     # Prometheus metrics
  state/
    store.py                   # StateStore: SQLite (stdlib sqlite3) with runs, phases, groups, candidates, actions, failures
    runtime_status.py          # in-memory live status (phase, progress, queue size)
    progress.py                # ProgressManager listener feeding the live status
  web/
    app.py                     # FastAPI app: JSON API under /api, /metrics, SPA static files + fallback
    thumbnails.py              # on-demand thumbnails (EXIF orientation applied), disk cache
    server.py                  # uvicorn in a background thread of the daemon
  library/
    deduplicator.py            # ImageMatchDeduplicator: the 6 phases, candidate selection & prioritization
    processing_manager.py      # daemon: file watchers + debounced processing queue
    file_watch.py              # watchdog event handler
    deduplication_result.py    # result model + console output
    progress_manager.py        # tqdm progress handling
  persistence/
    __init__.py                # ImageSignatureStore base class (metadata, EXIF, datamodel version)
    elasticsearchstorebackend.py  # Elasticsearch + image-match implementation
    metadata_key.py            # keys of stored documents
  util/                        # file + image helpers, echo()
tests/                         # pytest/unittest tests, test config + sample images
docker/entrypoint.sh           # drops privileges to PUID/PGID
py_image_dedup_reference.yaml  # documented reference configuration
```

## Architecture notes

- **Deduplication phases** (`ImageMatchDeduplicator.deduplicate_all`): DB cleanup, count files, analyse
  (signature + metadata into ES), find duplicates (ES similarity query), move/delete, remove empty folders.
  The README documents these phases; keep both in sync.
- **Configuration**: `DeduplicatorConfig` is a process-wide **singleton**. Sources are env vars
  (`PY_IMAGE_DEDUP_*`) and a `py_image_dedup.yaml` found in the **current working directory**
  (or the other default `container-app-conf` paths). Required values (e.g. `source_directories`) are
  validated on first instantiation. New options must be added to `config.py`, `py_image_dedup_reference.yaml`
  and the README.
- **Persistence**: `ImageSignatureStore` defines the store contract; `ElasticSearchStoreBackend` is the only
  implementation. Its constructor blocks (retry loop) until Elasticsearch answers. Bump
  `ImageSignatureStore.DATAMODEL_VERSION` when the stored document format changes — entries with another
  version are re-analysed.
- **image-match** is consumed from a fork (`markusressel/image-match`, git tag in `pyproject.toml`).
  Upstream is unmaintained. Changes to signature generation or the ES driver need a new tag in that fork.
- **Daemon**: `ProcessingManager` collects watchdog events into a queue and processes the affected
  *directories* after `processing_timeout` without new events (file-level processing is a TODO).
- **State store** (`py_image_dedup/state`): only used by the `daemon` command (`ImageMatchDeduplicator(state_store=...)`;
  without it the deduplicator behaves exactly as before). It records runs/phases, every duplicate group with its
  ranked candidates, the deciding prioritization rule (`_explain_decision`, groups decided by
  `WEAK_PRIORITIZATION_RULES` are flagged `needs_review`), user decisions and an action log (dry run actions are
  logged as `simulated`). Groups are identified by a hash of their member paths, so user decisions (ignore, chosen
  keeper) survive re-runs. The model is backend independent (`matcher` column) and meant to be reused by the
  planned matching & ranking pipeline. Store errors never break deduplication: a group that cannot be recorded is
  not touched.
- **Web UI decisions** are executed by the daemon main loop (`execute_user_decisions`, every second, holding
  `work_lock` so they never run concurrently with a deduplication run). They only ever move duplicates to
  `duplicates_target_directory` (never delete), respect `dry_run` and abort if the file to keep is missing.

## Testing

- Tests live in `tests/` and use `unittest.TestCase` via `tests.TestBase`, run with pytest.
- `tests/conftest.py` switches the working directory to `tests/` so `tests/py_image_dedup.yaml` is used.
  Do not rely on a `py_image_dedup.yaml` in the repo root (developers keep a private one there, untracked).
- `TestBase` patches `ElasticSearchStoreBackend`; unit tests must never need a running Elasticsearch.
  Put anything requiring a real ES instance behind the `integration` marker; `pytest.ini` excludes it from the default run.
- Integration tests (`tests/test_integration_elasticsearch.py`) run the real `ImageMatchDeduplicator` against
  Elasticsearch over `tests/images/` (dry run, separate index `py-image-dedup-integration-test`):
  ```shell
  docker run -d -p 9200:9200 -e discovery.type=single-node -e xpack.security.enabled=false elasticsearch:7.17.28
  poetry run pytest -m integration   # host/port via PY_IMAGE_DEDUP_ELASTICSEARCH_HOST / _PORT
  ```
  Tested Elasticsearch versions: 7.9.2 and 7.17.28 (both run in CI). Only Elasticsearch 7.x is supported
  (the client is pinned to `elasticsearch = "^7"`, 8.x is untested).
- Prioritization/selection logic (`_select_images_to_delete`, `_sort_by_quality_descending`, `_quality_criteria`)
  is the most safety-critical code: it decides which files get deleted. Cover changes there with tests.
- State store and API tests (`tests/test_state_store.py`, `tests/test_deduplicator_state.py`, `tests/test_web_api.py`)
  use an in-memory SQLite database and FastAPI's `TestClient`. Tests that move files work on copies in a temp directory.

## Safety rules

- This tool deletes user photos. `dry_run` defaults to `true`; keep that default.
- Never run the CLI or daemon against real photo directories while developing. Use `tests/images/`.
- Never commit personal config (`py_image_dedup.yaml` in the repo root), `.idea/` or `poetry.toml`.

## CI

GitHub Actions (`.github/workflows/`):

- `dockerimage-test.yml`: builds the Docker image on PRs (this is the main "does it still install" check).
- `dockerimage-latest.yml` / `dockerimage-release.yml`: build and push `markusressel/py-image-dedup` on master / tags.
- `tests.yml`: unit tests and integration tests (Elasticsearch service container) on PRs and pushes to master.
- `codespell.yml`: spelling (config in `pyproject.toml`, ignores in `.codespellignore`).
- `codeql-analysis.yml`: CodeQL.
- Dependabot opens PRs for pip and GitHub Actions updates.

## Conventions

- Follow the existing style: type hints where present, docstrings with `:param:` style, `echo()` from
  `py_image_dedup.util` for user-facing console output, `logging` for diagnostics.
- Keep changes focused; do not reformat unrelated code.
- Branch off `master` and open PRs against `master`.

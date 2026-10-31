"""
Glue between the deduplicator and the hash matching pipeline (py_image_dedup/matching), used with
matching.backend "hashes" or "both".
"""
import logging
import os
from pathlib import Path
from typing import Callable, Dict, List, Optional
from urllib.parse import urlparse

from py_image_dedup.config import DeduplicatorConfig
from py_image_dedup.matching.models import DuplicateGroup, Match
from py_image_dedup.matching.orientation import Orientation
from py_image_dedup.matching.pipeline import AnalysisResult, MatchingPipeline
from py_image_dedup.matching.store import SignatureStore, open_signature_store
from py_image_dedup.util.file import file_has_extension

LOGGER = logging.getLogger(__name__)


def create_embedding_stage(config: DeduplicatorConfig):
    """
    Creates the opt-in crop detection stage from matching.embeddings.* (model file, optional download).

    :return: the EmbeddingStage, None if matching.embeddings.enabled is false
    :raises ImportError: the 'embeddings' extra (onnxruntime) is not installed
    :raises ModelNotAvailableError: the model is missing, cannot be downloaded or has a wrong checksum
    :raises ValueError: model_url without model_sha256
    """
    if not config.MATCHING_EMBEDDINGS_ENABLED.value:
        return None
    from py_image_dedup.matching.embeddings import (
        DEFAULT_MODEL, EmbeddingStage, EmbeddingStore, ModelSource, OnnxEmbeddingExtractor, ensure_model,
    )

    cache_directory = config.embeddings_cache_directory
    cache_directory.mkdir(parents=True, exist_ok=True)
    model_path = config.MATCHING_EMBEDDINGS_MODEL_PATH.value
    expected_sha256 = config.MATCHING_EMBEDDINGS_MODEL_SHA256.value
    expected_sha256 = expected_sha256.lower() if expected_sha256 else None
    if model_path:
        model_path = Path(model_path).expanduser()
    else:
        url = config.MATCHING_EMBEDDINGS_MODEL_URL.value
        if url:
            if not expected_sha256:
                raise ValueError("matching.embeddings.model_url requires matching.embeddings.model_sha256")
            name = Path(urlparse(url).path).stem or "model"
            source = ModelSource(name=name, url=url, sha256=expected_sha256, size_bytes=0)
        else:
            source = DEFAULT_MODEL
            expected_sha256 = source.sha256
        model_path = ensure_model(source, cache_directory)

    extractor = OnnxEmbeddingExtractor(model_path, expected_sha256=expected_sha256)
    LOGGER.info(f"Crop detection enabled, model {model_path} ({extractor.model_id})")
    return EmbeddingStage(
        extractor,
        store=EmbeddingStore(cache_directory / "embeddings.db"),
        min_similarity=config.MATCHING_EMBEDDINGS_SIMILARITY_THRESHOLD.value,
        workers=config.ANALYSIS_THREADS.value,
    )


class HashMatcher:
    """
    Runs the hash matching pipeline over the configured source directories
    (respecting recursive, across_dirs, file_extensions and exclusions).
    """

    def __init__(self, config: DeduplicatorConfig, store: SignatureStore = None, embedding_stage=None):
        """
        :param config: the configuration
        :param store: signature store, default: opened from matching.store_url
        :param embedding_stage: opt-in crop detection, default: created from matching.embeddings.*
        """
        self._config = config
        if store is None:
            store = open_signature_store(config.signature_store_url)
        if embedding_stage is None:
            embedding_stage = create_embedding_stage(config)
        self.store = store
        self.pipeline = MatchingPipeline(
            store=store,
            thresholds=config.matching_thresholds(),
            workers=config.ANALYSIS_THREADS.value,
            embedding_stage=embedding_stage,
        )

    @property
    def embeddings_enabled(self) -> bool:
        return self.pipeline.embedding_stage is not None

    def find_files(self, directories: List[Path]) -> List[Path]:
        """
        :param directories: source directories
        :return: all image files to analyse, sorted
        """
        exclusions = self._config.EXCLUSIONS.value or []
        extensions = self._config.FILE_EXTENSION_FILTER.value
        result = set()
        for directory in directories:
            for root, _, files in os.walk(str(directory)):
                for name in files:
                    path = Path(root, name)
                    if any(pattern.search(str(path.absolute())) for pattern in exclusions):
                        continue
                    if not file_has_extension(path, extensions) or not path.is_file():
                        continue
                    result.add(path)
                if not self._config.RECURSIVE.value:
                    break
        return sorted(result)

    def analyse(self, files: List[Path], progress: Callable[[int, int], None] = None) -> AnalysisResult:
        """
        Computes the signatures of new/changed files, removes entries of files that are not in `files`.
        """
        return self.pipeline.analyse(files, progress)

    def match(self, directories: List[Path]) -> List[DuplicateGroup]:
        """
        :param directories: source directories (without across_dirs, only files of the same one are grouped)
        :return: duplicate groups
        """
        match_filter = None
        if not self._config.SEARCH_ACROSS_ROOT_DIRS.value:
            roots = [Path(d) for d in directories]

            def root_of(path: str) -> Optional[Path]:
                parents = Path(path).parents
                return next((root for root in roots if root in parents), None)

            def match_filter(match: Match) -> bool:
                root = root_of(match.a)
                return root is not None and root == root_of(match.b)

        result = self.pipeline.match(match_filter)
        LOGGER.info(f"Hash matching: {len(result.groups)} groups, timings {result.timings}")
        return result.groups


def member_match_info(group: DuplicateGroup) -> Dict[str, dict]:
    """
    Describes how each member of a group was matched: the strongest match the member is part of.

    :return: path -> {"variant_kind", "orientation", "match_confidence", "match_reason", "notes", "partner"}.
             orientation is the transform that turns the partner into this member ("identity" if upright).
    """
    info = {}
    for path in group.paths:
        incident = [m for m in group.matches if path in (m.a, m.b)]
        best: Optional[Match] = max(incident, key=lambda m: (m.confidence, m.b), default=None)
        kind = group.member_kinds.get(path)
        entry = {
            "variant_kind": kind.value if kind is not None else None,
            "orientation": None,
            "match_confidence": None,
            "match_reason": None,
            "notes": [],
            "partner": None,
        }
        if best is not None:
            partner = best.a if best.b == path else best.b
            # Match.orientation turns a into b
            orientation = best.orientation if best.b == path else best.orientation.inverse
            entry.update({
                "orientation": orientation.value if orientation is not Orientation.IDENTITY else None,
                "match_confidence": round(best.confidence, 4),
                "match_reason": best.reason.value,
                "notes": list(best.notes),
                "partner": partner,
            })
        info[path] = entry
    return info

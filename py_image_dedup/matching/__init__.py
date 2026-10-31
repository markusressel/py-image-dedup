"""
Experimental matching pipeline (prototype, see docs/matching-prototype.md).

Replaces image-match + Elasticsearch with local signatures (file hash, pixel hash, pHash, dHash) in SQLite,
staged grouping with a confidence per match, a metadata veto for burst shots and connected-component clustering.
Not wired into the deduplicator, CLI defaults or the daemon yet.
"""
from py_image_dedup.matching.grouping import burst_veto, cluster, find_matches
from py_image_dedup.matching.models import (
    DuplicateGroup, ImageMetadata, ImageSignature, Match, MatchReason, MatchingThresholds, Veto,
)
from py_image_dedup.matching.pipeline import MatchingPipeline, MatchingResult, find_image_files
from py_image_dedup.matching.signatures import compute_signature
from py_image_dedup.matching.store import SignatureStore
from py_image_dedup.matching.store import SqliteSignatureStore, open_signature_store

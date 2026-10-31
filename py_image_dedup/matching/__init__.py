"""
Experimental matching pipeline (prototype, see docs/matching-prototype.md).

Replaces image-match + Elasticsearch with local signatures (file hash, pixel hash, pHash, dHash) in SQLite,
staged grouping with a confidence and variant kind per match (rotation/mirror invariant), burst shots as a
confidence factor and connected-component clustering with an auto-act/review decision per group.
Not wired into the deduplicator, CLI defaults or the daemon yet.
"""
from py_image_dedup.matching.grouping import burst_note, classify_variant, cluster, find_matches
from py_image_dedup.matching.models import (
    DuplicateGroup, ImageMetadata, ImageSignature, Match, MatchReason, MatchingThresholds, VariantKind,
)
from py_image_dedup.matching.orientation import Orientation
from py_image_dedup.matching.pipeline import MatchingPipeline, MatchingResult, find_image_files
from py_image_dedup.matching.signatures import compute_signature
from py_image_dedup.matching.store import SignatureStore
from py_image_dedup.matching.store import SqliteSignatureStore, open_signature_store

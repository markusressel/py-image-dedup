"""
Hash matching pipeline (see docs/matching-prototype.md), used with matching.backend "hashes" or "both".

Replaces image-match + Elasticsearch with local signatures (file hash, pixel hash, pHash, dHash) in SQLite,
staged grouping with a confidence and variant kind per match (rotation/mirror invariant), burst shots as a
confidence factor and connected-component clustering with an auto-act/review decision per group.
The deduplicator runs it through py_image_dedup/library/hash_matcher.py.
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

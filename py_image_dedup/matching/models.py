from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional, Tuple

from py_image_dedup.matching.orientation import Orientation

# bump when the way signatures are computed changes, stored entries with another version are re-analysed
SIGNATURE_VERSION = 2


@dataclass(frozen=True)
class ImageMetadata:
    """
    Metadata relevant for matching, extracted from EXIF.
    """
    # DateTimeOriginal combined with SubSecTimeOriginal (naive, the camera's local time)
    capture_time: Optional[datetime] = None
    # True if SubSecTimeOriginal was present, i.e. capture_time is more precise than a second
    capture_time_has_subsec: bool = False
    camera_make: Optional[str] = None
    camera_model: Optional[str] = None
    image_unique_id: Optional[str] = None
    software: Optional[str] = None

    @property
    def camera(self) -> Optional[str]:
        """
        :return: "make model" or None if neither is known
        """
        parts = [p for p in (self.camera_make, self.camera_model) if p]
        return " ".join(parts) if parts else None


@dataclass(frozen=True)
class ImageSignature:
    """
    All signatures and metadata of a single image file.
    """
    path: str
    file_size: int
    # file modification time in nanoseconds, used to detect changed files
    mtime_ns: int
    # SHA-256 of the file content (hex)
    sha256: str
    # SHA-256 of the decoded, EXIF-oriented pixels of the (possibly JPEG draft reduced) preview image (hex),
    # only a pre-filter: equal preview hashes are verified with full_pixel_hash
    preview_pixel_hash: str
    # 64 bit perceptual hashes (imagehash), as unsigned int
    phash: int
    dhash: int
    # dimensions of the full image after applying the EXIF orientation
    width: int
    height: int
    # True if the image has (almost) no colour information
    is_grayscale: bool
    metadata: ImageMetadata = field(default_factory=ImageMetadata)
    # SHA-256 of the full resolution decoded pixels, only computed when needed (lazy)
    full_pixel_hash: Optional[str] = None
    signature_version: int = SIGNATURE_VERSION
    # (pHash, dHash) of the image rotated/mirrored by each of orientation.TRANSFORMS (in that order),
    # None if not computed (rotation/mirror matching disabled during analysis)
    orientation_hashes: Optional[Tuple[Tuple[int, int], ...]] = None
    # mean and standard deviation of the R, G and B channels of the preview (0..255), None if unknown,
    # used to tell colour-edited variants from plain copies
    colour_moments: Optional[Tuple[float, ...]] = None

    @property
    def aspect_ratio(self) -> float:
        """
        :return: width / height (always >= 1 for landscape)
        """
        return self.width / self.height if self.height else 0.0

    @property
    def pixel_count(self) -> int:
        """
        :return: number of pixels of the full image
        """
        return self.width * self.height


class MatchReason(Enum):
    """
    Why two files are considered duplicates, ordered by confidence (strongest first).
    """
    EXACT = "exact"  # identical file content
    PIXEL_IDENTICAL = "pixel-identical"  # identical decoded pixels, e.g. only metadata differs
    NEAR_DUPLICATE = "near-duplicate"  # perceptual hashes are close, e.g. recompressed, resized, slightly edited
    EMBEDDING = "embedding"  # opt-in: copy detection embeddings are similar, e.g. cropped (always reviewed)


class VariantKind(Enum):
    """
    How two matching files (or a group member and the rest of its group) relate to each other.
    """
    EXACT = "exact"  # identical file content
    PIXEL_IDENTICAL = "pixel-identical"  # identical decoded pixels
    COPY = "copy"  # recompressed and/or resized copy of the same content
    ROTATED = "rotated"  # rotated and/or mirrored copy (pixels, without orientation tag) of the same content
    GRAYSCALE = "grayscale"  # one copy is grayscale, the other is not
    COLOUR_EDITED = "colour-edited"  # colours/brightness/contrast differ (edited version)
    CROP = "crop"  # cropped: aspect ratio differs, or matched by the embedding stage only
    BURST = "burst"  # another shot of a series (same camera, capture times seconds apart), not a copy

    @property
    def needs_review(self) -> bool:
        """
        :return: True for variants a human has to decide about (they are usually kept next to the original)
        """
        return self in REVIEW_VARIANT_KINDS


REVIEW_VARIANT_KINDS = frozenset({VariantKind.GRAYSCALE, VariantKind.COLOUR_EDITED, VariantKind.CROP,
                                  VariantKind.BURST})


@dataclass(frozen=True)
class Match:
    """
    An accepted duplicate relation between two files (identified by their path, a < b).
    """
    a: str
    b: str
    reason: MatchReason
    # [0..1], how sure we are that both files show the same photo
    confidence: float
    phash_distance: int = 0
    dhash_distance: int = 0
    # human readable notes that influenced the confidence, e.g. "same capture time", "aspect ratio differs"
    notes: Tuple[str, ...] = ()
    # cosine similarity of the embeddings, only set for MatchReason.EMBEDDING
    embedding_similarity: Optional[float] = None
    # transform that turns a into b (rotation/mirror matching), IDENTITY for upright copies
    orientation: Orientation = Orientation.IDENTITY
    # how a and b differ
    kind: VariantKind = VariantKind.COPY


@dataclass(frozen=True)
class CropRelation:
    """
    One member of a group is a crop of another (fewer pixels, different aspect ratio or only matched by
    embeddings). Ranking hint: the crop is preferred by default, but such a group always goes to review.
    """
    crop: str
    source: str
    # pixels of the crop / pixels of the source
    area_ratio: float
    aspect_ratio_differs: bool
    # cosine similarity if the pair was matched by the embedding stage
    embedding_similarity: Optional[float] = None


@dataclass
class DuplicateGroup:
    """
    A connected component of matching files.
    """
    paths: List[str]
    # accepted matches between members of this group
    matches: List[Match]
    # lowest confidence of the matches that connect the group (the weakest link)
    confidence: float
    # strongest reason that applies to the weakest link
    reason: MatchReason
    # crop relations between members (see matching/embeddings.py), a group with crops must be reviewed
    crops: List[CropRelation] = field(default_factory=list)
    # path -> how this member relates to the group (BURST for burst shots, CROP for crops, otherwise the least
    # variant kind of its matches; a grayscale match only counts for the grayscale member)
    member_kinds: Dict[str, VariantKind] = field(default_factory=dict)
    # why the group must not be acted on automatically (variants, crops, burst shots, low confidence), empty if
    # automatic action is allowed
    review_reasons: List[str] = field(default_factory=list)

    @property
    def has_crops(self) -> bool:
        """
        :return: True if a member is a crop of another: never act automatically, rank the crop first
        """
        return bool(self.crops)

    @property
    def auto_act_allowed(self) -> bool:
        """
        :return: True if duplicates of this group may be moved/deleted without a human decision
        """
        return not self.review_reasons

    @property
    def review_reason(self) -> Optional[str]:
        """
        :return: all review reasons as one human readable string, None if automatic action is allowed
        """
        return "; ".join(self.review_reasons) if self.review_reasons else None


@dataclass
class MatchingThresholds:
    """
    Tunable parameters of the near-duplicate stage.
    """
    # maximum Hamming distance (of 64 bits) of the pHash
    max_phash_distance: int = 10
    # maximum Hamming distance (of 64 bits) of the dHash
    max_dhash_distance: int = 12
    # files of the same camera with capture times at most this many seconds apart (but not equal)
    # are treated as burst / series shots: they still match, but with lower confidence, and their group
    # is never acted on automatically
    burst_window_seconds: float = 10.0
    # confidence subtracted from a near-duplicate match between burst shots
    burst_confidence_penalty: float = 0.3
    # relative difference of the aspect ratios above which a match is flagged (e.g. cropped)
    aspect_ratio_tolerance: float = 0.02
    # largest difference of a channel mean or standard deviation (0..255) that still counts as the same
    # colours; above it a match is classified as colour-edited (recompressed/resized copies stay below 1)
    colour_edit_tolerance: float = 5.0
    # also match copies that were rotated/mirrored without an orientation tag (compares all 8 orientations)
    match_rotations: bool = True
    # groups with a lower confidence (weakest link) always go to review
    auto_act_min_confidence: float = 0.8

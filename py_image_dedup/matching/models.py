from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import List, Optional, Tuple

# bump when the way signatures are computed changes, stored entries with another version are re-analysed
SIGNATURE_VERSION = 1


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


@dataclass(frozen=True)
class Veto:
    """
    A candidate pair that was rejected although its signatures are similar.
    """
    a: str
    b: str
    reason: str
    phash_distance: int = 0
    dhash_distance: int = 0


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
    # matches that were not used because they would have joined vetoed files into one group
    conflicts: List[Match] = field(default_factory=list)


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
    # are treated as burst / series shots and never as duplicates
    burst_window_seconds: float = 10.0
    # relative difference of the aspect ratios above which a match is flagged (e.g. cropped)
    aspect_ratio_tolerance: float = 0.02

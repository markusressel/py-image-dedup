from collections import defaultdict
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from py_image_dedup.matching.models import (
    DuplicateGroup, ImageMetadata, ImageSignature, Match, MatchReason, MatchingThresholds, Veto,
)
from py_image_dedup.matching.search import find_candidate_pairs, to_array

CONFIDENCE_EXACT = 1.0
CONFIDENCE_PIXEL_IDENTICAL = 0.99
# near-duplicates are scored in [NEAR_MIN, NEAR_MAX] depending on the hash distances, then adjusted by notes
CONFIDENCE_NEAR_MIN = 0.5
CONFIDENCE_NEAR_MAX = 0.9
CONFIDENCE_NEAR_CAP = 0.98

BOOST_SAME_CAPTURE = 0.08
PENALTY_DIFFERENT_UNIQUE_ID = 0.1
PENALTY_ASPECT_RATIO = 0.15
PENALTY_COLOUR = 0.05

NOTE_SAME_CAPTURE = "same camera and capture time"
NOTE_SAME_UNIQUE_ID = "same ImageUniqueID"
NOTE_DIFFERENT_UNIQUE_ID = "different ImageUniqueID"
NOTE_ASPECT_RATIO = "aspect ratio differs (cropped?)"
NOTE_COLOUR = "colour differs (one copy is grayscale)"

# resolves the full resolution pixel hash of a file (expensive, called only for preview hash collisions)
FullPixelHashResolver = Callable[[ImageSignature], str]


def _pair(a: str, b: str) -> Tuple[str, str]:
    return (a, b) if a < b else (b, a)


def burst_veto(a: ImageMetadata, b: ImageMetadata, burst_window_seconds: float) -> Optional[str]:
    """
    Detects burst / series shots: the same camera took both photos within a few seconds.
    Copies of a photo keep the capture time, so a different capture time means a different shot.

    :param a: metadata of the first file
    :param b: metadata of the second file
    :param burst_window_seconds: maximum time difference that is considered a burst
    :return: a reason if the pair must not be matched, None otherwise
    """
    if a.camera is None or a.camera != b.camera:
        return None
    if a.capture_time is None or b.capture_time is None:
        return None
    time_a, time_b = a.capture_time, b.capture_time
    if not (a.capture_time_has_subsec and b.capture_time_has_subsec):
        # only compare with the precision both files have
        time_a, time_b = time_a.replace(microsecond=0), time_b.replace(microsecond=0)
    delta = abs((time_a - time_b).total_seconds())
    if 0 < delta <= burst_window_seconds:
        return f"burst: same camera ({a.camera}), captured {delta:.3g}s apart"
    return None


def same_capture(a: ImageMetadata, b: ImageMetadata) -> bool:
    """
    :param a: metadata of the first file
    :param b: metadata of the second file
    :return: True if both files were taken by the same camera at the same time
    """
    if a.camera is None or a.camera != b.camera or a.capture_time is None or b.capture_time is None:
        return False
    if a.capture_time_has_subsec and b.capture_time_has_subsec:
        return a.capture_time == b.capture_time
    return a.capture_time.replace(microsecond=0) == b.capture_time.replace(microsecond=0)


def near_duplicate_confidence(a: ImageSignature,
                              b: ImageSignature,
                              phash_distance: int,
                              dhash_distance: int,
                              thresholds: MatchingThresholds) -> Tuple[float, Tuple[str, ...]]:
    """
    Scores a near-duplicate candidate pair.

    :param a: first signature
    :param b: second signature
    :param phash_distance: pHash Hamming distance
    :param dhash_distance: dHash Hamming distance
    :param thresholds: matching thresholds
    :return: (confidence in [0..1], notes explaining adjustments)
    """
    closeness = 1 - (phash_distance / (thresholds.max_phash_distance + 1)
                     + dhash_distance / (thresholds.max_dhash_distance + 1)) / 2
    confidence = CONFIDENCE_NEAR_MIN + (CONFIDENCE_NEAR_MAX - CONFIDENCE_NEAR_MIN) * closeness
    notes = []

    meta_a, meta_b = a.metadata, b.metadata
    if meta_a.image_unique_id and meta_b.image_unique_id:
        if meta_a.image_unique_id == meta_b.image_unique_id:
            confidence += BOOST_SAME_CAPTURE
            notes.append(NOTE_SAME_UNIQUE_ID)
        else:
            confidence -= PENALTY_DIFFERENT_UNIQUE_ID
            notes.append(NOTE_DIFFERENT_UNIQUE_ID)
    elif same_capture(meta_a, meta_b):
        confidence += BOOST_SAME_CAPTURE
        notes.append(NOTE_SAME_CAPTURE)

    if a.aspect_ratio and b.aspect_ratio:
        relative_difference = abs(a.aspect_ratio - b.aspect_ratio) / max(a.aspect_ratio, b.aspect_ratio)
        if relative_difference > thresholds.aspect_ratio_tolerance:
            confidence -= PENALTY_ASPECT_RATIO
            notes.append(NOTE_ASPECT_RATIO)

    if a.is_grayscale != b.is_grayscale:
        confidence -= PENALTY_COLOUR
        notes.append(NOTE_COLOUR)

    return max(0.0, min(CONFIDENCE_NEAR_CAP, confidence)), tuple(notes)


def find_matches(signatures: Sequence[ImageSignature],
                 thresholds: MatchingThresholds = None,
                 full_pixel_hash_resolver: FullPixelHashResolver = None) -> Tuple[List[Match], List[Veto]]:
    """
    Staged matching. Every pair is tagged with its strongest reason:

    1. exact: identical SHA-256 of the file
    2. pixel-identical: identical decoded pixels (preview pixel hash, verified with the full resolution
       pixel hash if a resolver is given; with a resolver, near-duplicate candidates with hash distance 0 and
       the same dimensions are checked as well, e.g. a lossless PNG of a JPEG)
    3. near-duplicate: pHash and dHash within the thresholds, unless vetoed (burst shots)

    :param signatures: signatures of all files to compare (paths must be unique)
    :param thresholds: matching thresholds, defaults if None
    :param full_pixel_hash_resolver: computes the full resolution pixel hash, if None the preview
                                     pixel hash alone decides
    :return: (accepted matches, vetoed candidate pairs)
    """
    thresholds = thresholds or MatchingThresholds()
    matches: Dict[Tuple[str, str], Match] = {}

    # stage 1: exact
    by_sha = defaultdict(list)
    for signature in signatures:
        by_sha[signature.sha256].append(signature)
    for members in by_sha.values():
        first = members[0]
        for other in members[1:]:
            pair = _pair(first.path, other.path)
            matches[pair] = Match(*pair, reason=MatchReason.EXACT, confidence=CONFIDENCE_EXACT)

    # stage 2: pixel-identical (one representative per distinct file content)
    by_preview = defaultdict(list)
    for members in by_sha.values():
        by_preview[members[0].preview_pixel_hash].append(members[0])
    pixel_class: Dict[str, str] = {}  # sha256 -> key of its pixel-identical class
    for preview_hash, representatives in by_preview.items():
        if len(representatives) < 2:
            continue
        by_pixels = defaultdict(list)
        for representative in representatives:
            key = full_pixel_hash_resolver(representative) if full_pixel_hash_resolver else preview_hash
            by_pixels[key].append(representative)
        for key, identical in by_pixels.items():
            for representative in identical:
                pixel_class[representative.sha256] = key
            first = identical[0]
            for other in identical[1:]:
                pair = _pair(first.path, other.path)
                matches[pair] = Match(*pair, reason=MatchReason.PIXEL_IDENTICAL, confidence=CONFIDENCE_PIXEL_IDENTICAL)

    # stage 3: near-duplicates
    vetoes = []
    candidates = find_candidate_pairs(
        to_array([s.phash for s in signatures]), to_array([s.dhash for s in signatures]),
        thresholds.max_phash_distance, thresholds.max_dhash_distance)
    for candidate in candidates:
        a, b = signatures[candidate.i], signatures[candidate.j]
        if a.sha256 == b.sha256:
            continue
        class_a, class_b = pixel_class.get(a.sha256), pixel_class.get(b.sha256)
        if class_a is not None and class_a == class_b:
            continue
        pair = _pair(a.path, b.path)
        if (full_pixel_hash_resolver is not None and candidate.phash_distance == 0 and candidate.dhash_distance == 0
                and (a.width, a.height) == (b.width, b.height)
                and full_pixel_hash_resolver(a) == full_pixel_hash_resolver(b)):
            # e.g. a lossless PNG of a JPEG: preview hashes differ because only JPEG is decoded in draft mode
            matches[pair] = Match(*pair, reason=MatchReason.PIXEL_IDENTICAL, confidence=CONFIDENCE_PIXEL_IDENTICAL)
            continue
        veto_reason = burst_veto(a.metadata, b.metadata, thresholds.burst_window_seconds)
        if veto_reason is not None:
            vetoes.append(Veto(*pair, reason=veto_reason, phash_distance=candidate.phash_distance,
                               dhash_distance=candidate.dhash_distance))
            continue
        confidence, notes = near_duplicate_confidence(a, b, candidate.phash_distance, candidate.dhash_distance,
                                                      thresholds)
        matches[pair] = Match(*pair, reason=MatchReason.NEAR_DUPLICATE, confidence=confidence,
                              phash_distance=candidate.phash_distance, dhash_distance=candidate.dhash_distance,
                              notes=notes)

    return list(matches.values()), vetoes


class _ConstrainedUnionFind:
    """
    Union-find whose unions are refused if they would put two vetoed files into one set.
    """

    def __init__(self, items: Iterable[str], is_vetoed: Callable[[str, str], bool]):
        self._parent = {item: item for item in items}
        self._members = {item: [item] for item in self._parent}
        self._is_vetoed = is_vetoed

    def find(self, item: str) -> str:
        root = item
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[item] != root:
            self._parent[item], item = root, self._parent[item]
        return root

    def union(self, a: str, b: str) -> Optional[bool]:
        """
        :return: True if two sets were joined, None if both already are in the same set,
                 False if the union was refused
        """
        root_a, root_b = self.find(a), self.find(b)
        if root_a == root_b:
            return None
        members_a, members_b = self._members[root_a], self._members[root_b]
        if any(self._is_vetoed(x, y) for x in members_a for y in members_b):
            return False
        if len(members_a) < len(members_b):
            root_a, root_b = root_b, root_a
        self._parent[root_b] = root_a
        self._members[root_a].extend(self._members.pop(root_b))
        return True

    def sets(self) -> List[List[str]]:
        return [members for root, members in self._members.items() if self._parent[root] == root]


def cluster(signatures: Sequence[ImageSignature],
            matches: Sequence[Match],
            thresholds: MatchingThresholds = None) -> List[DuplicateGroup]:
    """
    Clusters matches into groups (connected components), strongest matches first.

    A match is not used if it would join two files that are vetoed as burst shots (also when they are not
    a direct candidate pair, e.g. A~B and B~C but A/C is a burst). Such matches are reported as conflicts.

    :param signatures: signatures of all files referenced by the matches
    :param matches: accepted matches
    :param thresholds: matching thresholds (burst window), defaults if None
    :return: groups with at least two files, largest first
    """
    thresholds = thresholds or MatchingThresholds()
    metadata = {signature.path: signature.metadata for signature in signatures}

    def is_vetoed(x: str, y: str) -> bool:
        return burst_veto(metadata[x], metadata[y], thresholds.burst_window_seconds) is not None

    involved = sorted({path for match in matches for path in (match.a, match.b)})
    union_find = _ConstrainedUnionFind(involved, is_vetoed)
    ordered = sorted(matches, key=lambda m: (-m.confidence, m.a, m.b))
    joining: List[Match] = []
    redundant: List[Match] = []
    conflicts: List[Match] = []
    for match in ordered:
        joined = union_find.union(match.a, match.b)
        if joined is True:
            joining.append(match)
        elif joined is None:
            redundant.append(match)
        else:
            conflicts.append(match)

    groups: Dict[str, DuplicateGroup] = {}
    for members in union_find.sets():
        if len(members) < 2:
            continue
        root = union_find.find(members[0])
        groups[root] = DuplicateGroup(paths=sorted(members), matches=[], confidence=1.0, reason=MatchReason.EXACT)

    for match in joining:
        group = groups[union_find.find(match.a)]
        group.matches.append(match)
        if match.confidence <= group.confidence:
            group.confidence = match.confidence
            group.reason = match.reason
    for match in redundant:
        groups[union_find.find(match.a)].matches.append(match)
    for match in conflicts:
        for path in (match.a, match.b):
            group = groups.get(union_find.find(path))
            if group is not None and match not in group.conflicts:
                group.conflicts.append(match)

    return sorted(groups.values(), key=lambda g: (-len(g.paths), g.paths[0]))

import os
from collections import defaultdict
from itertools import combinations
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from py_image_dedup.matching.models import (
    DuplicateGroup, ImageMetadata, ImageSignature, Match, MatchReason, MatchingThresholds, VariantKind,
)
from py_image_dedup.matching.orientation import Orientation, TRANSFORMS
from py_image_dedup.matching.search import CandidatePair, find_candidate_pairs, to_array

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
NOTE_COLOUR_EDITED = "colours differ (edited?)"

# variant kinds that are reported as review reasons of a group, in this order (bursts are reported separately)
_REVIEW_REASON_KINDS = (VariantKind.CROP, VariantKind.GRAYSCALE, VariantKind.COLOUR_EDITED)

# resolves the full resolution pixel hash of a file (expensive, called only for preview hash collisions)
FullPixelHashResolver = Callable[[ImageSignature], str]


def _pair(a: str, b: str) -> Tuple[str, str]:
    return (a, b) if a < b else (b, a)


def burst_delta(a: ImageMetadata, b: ImageMetadata, burst_window_seconds: float) -> Optional[float]:
    """
    Detects burst / series shots: the same camera took both photos within a few seconds.
    Copies of a photo keep the capture time, so a different capture time means a different shot
    (or a copy whose capture time was edited).

    :param a: metadata of the first file
    :param b: metadata of the second file
    :param burst_window_seconds: maximum time difference that is considered a burst
    :return: the time difference in seconds if both are burst shots, None otherwise
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
        return delta
    return None


def burst_note(a: ImageMetadata, b: ImageMetadata, burst_window_seconds: float) -> Optional[str]:
    """
    :param a: metadata of the first file
    :param b: metadata of the second file
    :param burst_window_seconds: maximum time difference that is considered a burst
    :return: a human readable note (e.g. "burst: same camera (Google Pixel 7), 3 s apart") if both files are
             burst shots, None otherwise
    """
    delta = burst_delta(a, b, burst_window_seconds)
    if delta is None:
        return None
    return f"burst: same camera ({a.camera}), {delta:.3g} s apart"


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


def aspect_ratio_differs(a: ImageSignature, b: ImageSignature, tolerance: float,
                         orientation: Orientation = Orientation.IDENTITY) -> bool:
    """
    :param a: first signature
    :param b: second signature
    :param tolerance: maximum relative difference of the aspect ratios
    :param orientation: transform that turns a into b (90° rotations swap width and height)
    :return: True if the aspect ratios differ (e.g. one copy is cropped)
    """
    if not (a.aspect_ratio and b.aspect_ratio):
        return False
    ratio_a = 1 / a.aspect_ratio if orientation.transposes else a.aspect_ratio
    return abs(ratio_a - b.aspect_ratio) / max(ratio_a, b.aspect_ratio) > tolerance


def colour_difference(a: ImageSignature, b: ImageSignature) -> Optional[float]:
    """
    :param a: first signature
    :param b: second signature
    :return: largest difference of a channel mean or standard deviation (0..255), None if unknown
    """
    if a.colour_moments is None or b.colour_moments is None:
        return None
    return max(abs(x - y) for x, y in zip(a.colour_moments, b.colour_moments))


def classify_variant(a: ImageSignature,
                     b: ImageSignature,
                     thresholds: MatchingThresholds,
                     orientation: Orientation = Orientation.IDENTITY) -> VariantKind:
    """
    Classifies how two near-duplicate files differ. The first kind that applies wins:
    burst, crop, grayscale, colour-edited, rotated, copy.

    :param a: first signature
    :param b: second signature
    :param thresholds: matching thresholds
    :param orientation: transform that turns a into b
    :return: the variant kind
    """
    if burst_delta(a.metadata, b.metadata, thresholds.burst_window_seconds) is not None:
        return VariantKind.BURST
    if aspect_ratio_differs(a, b, thresholds.aspect_ratio_tolerance, orientation):
        return VariantKind.CROP
    if a.is_grayscale != b.is_grayscale:
        return VariantKind.GRAYSCALE
    difference = colour_difference(a, b)
    if difference is not None and difference > thresholds.colour_edit_tolerance:
        return VariantKind.COLOUR_EDITED
    if orientation is not Orientation.IDENTITY:
        return VariantKind.ROTATED
    return VariantKind.COPY


def near_duplicate_confidence(a: ImageSignature,
                              b: ImageSignature,
                              phash_distance: int,
                              dhash_distance: int,
                              thresholds: MatchingThresholds,
                              orientation: Orientation = Orientation.IDENTITY) -> Tuple[float, Tuple[str, ...]]:
    """
    Scores a near-duplicate candidate pair.

    :param a: first signature
    :param b: second signature
    :param phash_distance: pHash Hamming distance
    :param dhash_distance: dHash Hamming distance
    :param thresholds: matching thresholds
    :param orientation: transform that turns a into b (rotation/mirror matching)
    :return: (confidence in [0..1], notes explaining adjustments)
    """
    closeness = 1 - (phash_distance / (thresholds.max_phash_distance + 1)
                     + dhash_distance / (thresholds.max_dhash_distance + 1)) / 2
    confidence = CONFIDENCE_NEAR_MIN + (CONFIDENCE_NEAR_MAX - CONFIDENCE_NEAR_MIN) * closeness
    notes = []
    if orientation is not Orientation.IDENTITY:
        notes.append(orientation.value)

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

    burst = burst_note(meta_a, meta_b, thresholds.burst_window_seconds)
    if burst is not None:
        confidence -= thresholds.burst_confidence_penalty
        notes.append(burst)

    if aspect_ratio_differs(a, b, thresholds.aspect_ratio_tolerance, orientation):
        confidence -= PENALTY_ASPECT_RATIO
        notes.append(NOTE_ASPECT_RATIO)

    if a.is_grayscale != b.is_grayscale:
        confidence -= PENALTY_COLOUR
        notes.append(NOTE_COLOUR)
    else:
        difference = colour_difference(a, b)
        if difference is not None and difference > thresholds.colour_edit_tolerance:
            notes.append(NOTE_COLOUR_EDITED)

    return max(0.0, min(CONFIDENCE_NEAR_CAP, confidence)), tuple(notes)


def _near_duplicate_match(a: ImageSignature, b: ImageSignature, candidate: CandidatePair,
                          thresholds: MatchingThresholds, orientation: Orientation = Orientation.IDENTITY) -> Match:
    """
    :param orientation: transform that turns a into b
    :return: a near-duplicate match of a and b (ordered by path)
    """
    if b.path < a.path:
        a, b, orientation = b, a, orientation.inverse
    confidence, notes = near_duplicate_confidence(a, b, candidate.phash_distance, candidate.dhash_distance,
                                                  thresholds, orientation)
    return Match(a.path, b.path, reason=MatchReason.NEAR_DUPLICATE, confidence=confidence,
                 phash_distance=candidate.phash_distance, dhash_distance=candidate.dhash_distance, notes=notes,
                 orientation=orientation, kind=classify_variant(a, b, thresholds, orientation))


def find_matches(signatures: Sequence[ImageSignature],
                 thresholds: MatchingThresholds = None,
                 full_pixel_hash_resolver: FullPixelHashResolver = None) -> List[Match]:
    """
    Staged matching. Every pair is tagged with its strongest reason:

    1. exact: identical SHA-256 of the file
    2. pixel-identical: identical decoded pixels (preview pixel hash, verified with the full resolution
       pixel hash if a resolver is given; with a resolver, near-duplicate candidates with hash distance 0 and
       the same dimensions are checked as well, e.g. a lossless PNG of a JPEG)
    3. near-duplicate: pHash and dHash within the thresholds; if `thresholds.match_rotations` is set, also
       between a rotated/mirrored orientation of one file and the other file (the upright comparison wins)

    Every match is classified (`Match.kind`): burst shots (same camera, capture times within the burst window)
    still match, with a lower confidence and a note, so that their group goes to review.

    :param signatures: signatures of all files to compare (paths must be unique)
    :param thresholds: matching thresholds, defaults if None
    :param full_pixel_hash_resolver: computes the full resolution pixel hash, if None the preview
                                     pixel hash alone decides
    :return: accepted matches
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
            matches[pair] = Match(*pair, reason=MatchReason.EXACT, confidence=CONFIDENCE_EXACT,
                                  kind=VariantKind.EXACT)

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
                matches[pair] = Match(*pair, reason=MatchReason.PIXEL_IDENTICAL,
                                      confidence=CONFIDENCE_PIXEL_IDENTICAL, kind=VariantKind.PIXEL_IDENTICAL)

    def is_same_content(a: ImageSignature, b: ImageSignature) -> bool:
        if a.sha256 == b.sha256:
            return True
        class_a = pixel_class.get(a.sha256)
        return class_a is not None and class_a == pixel_class.get(b.sha256)

    # stage 3: near-duplicates
    phashes, dhashes = to_array([s.phash for s in signatures]), to_array([s.dhash for s in signatures])
    # pairs decided by the upright comparison, never re-evaluated in another orientation
    upright_pairs = set()
    candidates = find_candidate_pairs(phashes, dhashes, thresholds.max_phash_distance, thresholds.max_dhash_distance)
    for candidate in candidates:
        a, b = signatures[candidate.i], signatures[candidate.j]
        pair = _pair(a.path, b.path)
        upright_pairs.add(pair)
        if is_same_content(a, b):
            continue
        if (full_pixel_hash_resolver is not None and candidate.phash_distance == 0 and candidate.dhash_distance == 0
                and (a.width, a.height) == (b.width, b.height)
                and full_pixel_hash_resolver(a) == full_pixel_hash_resolver(b)):
            # e.g. a lossless PNG of a JPEG: preview hashes differ because only JPEG is decoded in draft mode
            matches[pair] = Match(*pair, reason=MatchReason.PIXEL_IDENTICAL, confidence=CONFIDENCE_PIXEL_IDENTICAL,
                                  kind=VariantKind.PIXEL_IDENTICAL)
            continue
        matches[pair] = _near_duplicate_match(a, b, candidate, thresholds)

    # stage 3b: rotated/mirrored near-duplicates: the transformed hashes of file i are compared with the upright
    # hashes of every file j > i. Comparing only i < j is enough because the inverse of every transform is
    # one of the transforms as well (b = T(a) <=> a = T⁻¹(b)).
    if thresholds.match_rotations:
        best: Dict[Tuple[str, str], Tuple[int, CandidatePair, Orientation]] = {}
        for index, orientation in enumerate(TRANSFORMS):
            # files without orientation hashes are compared upright again, which yields no new pairs
            query_phashes = to_array([s.orientation_hashes[index][0] if s.orientation_hashes else s.phash
                                      for s in signatures])
            query_dhashes = to_array([s.orientation_hashes[index][1] if s.orientation_hashes else s.dhash
                                      for s in signatures])
            for candidate in find_candidate_pairs(phashes, dhashes, thresholds.max_phash_distance,
                                                  thresholds.max_dhash_distance,
                                                  query_phashes=query_phashes, query_dhashes=query_dhashes):
                a, b = signatures[candidate.i], signatures[candidate.j]
                pair = _pair(a.path, b.path)
                if a.orientation_hashes is None or pair in upright_pairs or pair in matches \
                        or is_same_content(a, b):
                    continue
                score = candidate.phash_distance + candidate.dhash_distance
                if pair not in best or score < best[pair][0]:
                    best[pair] = (score, candidate, orientation)
        for pair, (_, candidate, orientation) in best.items():
            matches[pair] = _near_duplicate_match(signatures[candidate.i], signatures[candidate.j], candidate,
                                                  thresholds, orientation)

    return list(matches.values())


class _UnionFind:
    """
    Union-find over paths that keeps the members of every set.
    """

    def __init__(self, items: Iterable[str]):
        self._parent = {item: item for item in items}
        self._members = {item: [item] for item in self._parent}

    def find(self, item: str) -> str:
        root = item
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[item] != root:
            self._parent[item], item = root, self._parent[item]
        return root

    def union(self, a: str, b: str) -> bool:
        """
        :return: True if two sets were joined, False if both already are in the same set
        """
        root_a, root_b = self.find(a), self.find(b)
        if root_a == root_b:
            return False
        if len(self._members[root_a]) < len(self._members[root_b]):
            root_a, root_b = root_b, root_a
        self._parent[root_b] = root_a
        self._members[root_a].extend(self._members.pop(root_b))
        return True

    def sets(self) -> List[List[str]]:
        return [members for root, members in self._members.items() if self._parent[root] == root]


def _member_kinds(group: DuplicateGroup, signatures: Dict[str, ImageSignature],
                  burst_members: Set[str]) -> Dict[str, VariantKind]:
    """
    A member is only as much of a variant as its closest relation: an edited copy differs from every other
    member (all its matches are colour-edited), while the original also has plain copies.

    :return: path -> BURST for burst shots, otherwise the least variant kind (`VariantKind` order) of the
             member's matches; a grayscale match only counts for its grayscale member (the colour member uses
             its other matches, if it has any)
    """
    order = {kind: index for index, kind in enumerate(VariantKind)}
    by_member: Dict[str, List[VariantKind]] = defaultdict(list)
    for match in group.matches:
        by_member[match.a].append(match.kind)
        by_member[match.b].append(match.kind)
    result = {}
    for path in group.paths:
        if path in burst_members:
            result[path] = VariantKind.BURST
            continue
        kinds = by_member[path]
        if not signatures[path].is_grayscale:
            kinds = [k for k in kinds if k is not VariantKind.GRAYSCALE] or kinds
        result[path] = min(kinds, key=order.__getitem__)
    return result


def _bursts(group: DuplicateGroup, signatures: Dict[str, ImageSignature],
            thresholds: MatchingThresholds) -> Tuple[List[str], Set[str]]:
    """
    Checks every pair of members, also those only connected through other files
    (A ~ B ~ C with A/C burst shots and B a messenger copy without metadata).

    :return: (sorted distinct burst notes, paths of the members that are burst shots)
    """
    notes, members = set(), set()
    for x, y in combinations(group.paths, 2):
        note = burst_note(signatures[x].metadata, signatures[y].metadata, thresholds.burst_window_seconds)
        if note is not None:
            notes.add(note)
            members.update((x, y))
    return sorted(notes), members


def variant_reason(kind: VariantKind, paths: Iterable[str]) -> str:
    """
    :param kind: variant kind
    :param paths: the members that are such a variant
    :return: review reason, e.g. "grayscale variant: a.jpg, b.jpg"
    """
    return f"{kind.value} variant: {', '.join(os.path.basename(p) for p in paths)}"


def _review_reasons(group: DuplicateGroup, burst_notes: List[str], thresholds: MatchingThresholds) -> List[str]:
    """
    :return: why the group must go to review, empty if it may be acted on automatically
    """
    reasons = list(burst_notes)
    for kind in _REVIEW_REASON_KINDS:
        kind_matches = [m for m in group.matches if m.kind is kind]
        if not kind_matches:
            continue
        members = sorted(p for p, k in group.member_kinds.items() if k is kind)
        if not members:
            members = sorted({p for m in kind_matches for p in (m.a, m.b)})
        reasons.append(variant_reason(kind, members))

    if group.confidence < thresholds.auto_act_min_confidence:
        reasons.append(f"confidence {group.confidence:.2f} below {thresholds.auto_act_min_confidence:.2f}")
    return reasons


def cluster(signatures: Sequence[ImageSignature],
            matches: Sequence[Match],
            thresholds: MatchingThresholds = None) -> List[DuplicateGroup]:
    """
    Clusters matches into groups (connected components), strongest matches first, and decides whether a group
    may be acted on automatically: groups with variants (grayscale, colour-edited, crop), burst shots
    (also indirectly connected ones) or a confidence below `thresholds.auto_act_min_confidence` go to review.

    :param signatures: signatures of all files referenced by the matches
    :param matches: accepted matches
    :param thresholds: matching thresholds, defaults if None
    :return: groups with at least two files, largest first
    """
    thresholds = thresholds or MatchingThresholds()
    by_path = {signature.path: signature for signature in signatures}

    involved = sorted({path for match in matches for path in (match.a, match.b)})
    union_find = _UnionFind(involved)
    ordered = sorted(matches, key=lambda m: (-m.confidence, m.a, m.b))
    joining: List[Match] = []
    redundant: List[Match] = []
    for match in ordered:
        (joining if union_find.union(match.a, match.b) else redundant).append(match)

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

    for group in groups.values():
        burst_notes, burst_members = _bursts(group, by_path, thresholds)
        group.member_kinds = _member_kinds(group, by_path, burst_members)
        group.review_reasons = _review_reasons(group, burst_notes, thresholds)

    return sorted(groups.values(), key=lambda g: (-len(g.paths), g.paths[0]))

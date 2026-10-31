"""
Benchmark of the experimental matching pipeline (py_image_dedup.matching), see docs/matching-prototype.md.

Runs the pipeline on a directory and prints groups, timings and memory usage. Optionally compares the
result with image-match (Goldberg signature): offline (pairwise distances, no Elasticsearch needed) and/or
through the production Elasticsearch backend.

Only run this on test data (e.g. tests/images/), it never modifies files.

Examples:

    poetry run python -m py_image_dedup.matching.benchmark tests/images --ground-truth folders --compare-goldberg
    poetry run python -m py_image_dedup.matching.benchmark tests/images --ground-truth folders \\
        --compare-elasticsearch 127.0.0.1:9200
    poetry run python -m py_image_dedup.matching.benchmark --synthetic 40000
    poetry run python -m py_image_dedup.matching.benchmark tests/images --robustness

`--robustness` generates variants (copies, recompressions, rotations, crops, a simulated burst shot) of every
original (file name without "-", tests/images layout) into a temporary directory and evaluates them with
rotation/mirror matching on and off.
"""
import argparse
import random
import resource
import shutil
import tempfile
import time
import tracemalloc
from dataclasses import replace
from datetime import datetime, timedelta
from itertools import combinations
from pathlib import Path
from typing import Dict, List, Set, Tuple

from PIL import Image, ImageEnhance, ImageOps

from py_image_dedup.matching import (
    ImageSignature, MatchingPipeline, MatchingThresholds, SignatureStore, cluster, compute_signature, find_image_files,
    find_matches,
)
from py_image_dedup.matching.decoding import EXIF_ORIENTATION
from py_image_dedup.matching.evaluation import (
    PairMetrics, evaluate_pairs, ground_truth_by_folder, pairs_of_groups,
)
from py_image_dedup.matching.metadata import IFD_EXIF, TAG_DATETIME_ORIGINAL
from py_image_dedup.matching.orientation import TRANSFORMS
from py_image_dedup.matching.search import find_candidate_pairs, hamming_distance, to_array

Pair = Tuple[str, str]


def _max_rss_mb() -> float:
    # ru_maxrss is in KiB on Linux
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def _short(path: str, root: Path) -> str:
    try:
        return str(Path(path).relative_to(root))
    except ValueError:
        return path


def _print_metrics(name: str, metrics: PairMetrics):
    print(f"  {name:<40} precision {metrics.precision:6.1%}  recall {metrics.recall:6.1%}  "
          f"(tp {metrics.true_positives}, fp {metrics.false_positives}, fn {metrics.false_negatives})")


def _thresholds(args) -> MatchingThresholds:
    return MatchingThresholds(
        max_phash_distance=args.max_phash,
        max_dhash_distance=args.max_dhash,
        burst_window_seconds=args.burst_window,
        match_rotations=not args.no_rotations,
    )


def run_pipeline(directory: Path, args) -> Tuple[List[str], Set[Pair], MatchingPipeline, List]:
    thresholds = _thresholds(args)
    store = SignatureStore(args.db)
    pipeline = MatchingPipeline(store, thresholds, workers=args.workers, preview_size=args.preview_size,
                                verify_pixel_identical=not args.no_verify)

    files = find_image_files(directory)
    print(f"Files: {len(files)} in {directory}")

    tracemalloc.start()
    start = time.perf_counter()
    analysis = pipeline.analyse(files)
    analyse_seconds = time.perf_counter() - start
    _, analyse_peak = tracemalloc.get_traced_memory()
    tracemalloc.reset_peak()

    start = time.perf_counter()
    result = pipeline.match()
    match_seconds = time.perf_counter() - start
    _, match_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    print()
    print("Groups:")
    print_groups(result.groups, directory)

    print()
    print("Timings:")
    print(f"  analyse    {analyse_seconds:8.3f}s  ({analysis.analysed} analysed, {analysis.skipped_unchanged} unchanged, "
          f"{len(analysis.failed)} failed, {args.workers or 'cpu count'} workers"
          f", {analyse_seconds / max(analysis.analysed, 1) * 1000:.1f} ms/file wall)")
    for stage, seconds in result.timings.items():
        print(f"  {stage:<10} {seconds:8.3f}s")
    print(f"  total match {match_seconds:7.3f}s")
    print("Memory:")
    print(f"  traced peak analyse {analyse_peak / 1e6:.1f} MB, match {match_peak / 1e6:.1f} MB, "
          f"max RSS {_max_rss_mb():.0f} MB")

    found = pairs_of_groups(group.paths for group in result.groups)
    return [str(f) for f in files], found, pipeline, result.groups


def print_groups(groups, directory: Path):
    for group in groups:
        decision = "auto-act" if group.auto_act_allowed else f"REVIEW: {group.review_reason}"
        print(f"  [{group.reason.value}, confidence {group.confidence:.2f}] {len(group.paths)} files, {decision}")
        for path in group.paths:
            print(f"    {group.member_kinds[path].value:<15} {_short(path, directory)}")
        for match in sorted(group.matches, key=lambda m: -m.confidence):
            notes = f" ({', '.join(match.notes)})" if match.notes else ""
            print(f"      {_short(match.a, directory)} ~ {_short(match.b, directory)}: {match.kind.value} "
                  f"{match.confidence:.2f} pHash {match.phash_distance} dHash {match.dhash_distance}{notes}")


def distance_sweep(paths: List[str], store: SignatureStore, expected: Set[Pair]):
    """
    Prints pHash/dHash distances of all positive and the closest negative pairs, to choose thresholds.
    """
    signatures = {s.path: s for s in store.get_all()}
    positive, negative = [], []
    for a, b in combinations(sorted(paths), 2):
        sa, sb = signatures[a], signatures[b]
        distances = (hamming_distance(sa.phash, sb.phash), hamming_distance(sa.dhash, sb.dhash))
        (positive if (a, b) in expected else negative).append(distances)
    if positive:
        print(f"  positives: max pHash {max(p for p, _ in positive)}, max dHash {max(d for _, d in positive)}")
    if negative:
        print(f"  negatives: min pHash {min(p for p, _ in negative)}, min dHash {min(d for _, d in negative)}")


def goldberg_pairs(paths: List[str], max_distance: float) -> Tuple[Set[Pair], Dict[Pair, float], float]:
    """
    Pairwise image-match (Goldberg) distances without Elasticsearch, i.e. an upper bound of what the
    Elasticsearch word pre-filter can return.
    """
    from image_match.goldberg import ImageSignature as GoldbergSignature
    goldberg = GoldbergSignature()
    start = time.perf_counter()
    signatures = {path: goldberg.generate_signature(path) for path in paths}
    seconds = time.perf_counter() - start
    distances = {}
    for a, b in combinations(sorted(paths), 2):
        distances[(a, b)] = float(goldberg.normalized_distance(signatures[a], signatures[b]))
    found = {pair for pair, distance in distances.items() if distance <= max_distance}
    return found, distances, seconds


def elasticsearch_pairs(paths: List[str], host_port: str, max_distance: float, index: str) -> Tuple[Set[Pair], float]:
    """
    Runs the production image-match + Elasticsearch backend (add all files, query each one).
    The index is deleted before and afterwards.
    """
    import requests
    from py_image_dedup.persistence.elasticsearchstorebackend import ElasticSearchStoreBackend

    host, port = host_port.rsplit(":", 1)
    url = f"http://{host}:{port}/{index}"
    requests.delete(url, timeout=10)
    try:
        backend = ElasticSearchStoreBackend(host=host, port=int(port), connections_per_node=4, el_index=index,
                                            max_dist=max_distance)
        start = time.perf_counter()
        for path in paths:
            backend.add(path)
        requests.post(f"{url}/_refresh", timeout=10).raise_for_status()
        found = set()
        for path in paths:
            for entry in backend.find_similar(path):
                other = entry.get("path")
                if other and other != path:
                    found.add((path, other) if path < other else (other, path))
        return found, time.perf_counter() - start
    finally:
        requests.delete(url, timeout=10)


def _strip_exif(source: Path, target: Path):
    """
    Copies a JPEG without its APP1 (EXIF/XMP) segments, keeping the compressed image data unchanged.
    """
    data = source.read_bytes()
    output, position = bytearray(data[:2]), 2
    while position < len(data):
        marker = data[position + 1]
        if marker == 0xDA:  # start of scan: the rest is image data
            output += data[position:]
            break
        length = int.from_bytes(data[position + 2:position + 4], "big")
        if marker != 0xE1:
            output += data[position:position + 2 + length]
        position += 2 + length
    target.write_bytes(bytes(output))


def _orientation_variant(orientation) -> str:
    return "orientation-" + orientation.name.lower().replace("_", "-")


EXPECT_COPY = "copy"  # same content: expected in the group of the original, may be acted on automatically
EXPECT_VARIANT = "variant"  # edited version of the original: expected in its group, but only for review
EXPECT_DIFFERENT = "different"  # another photo (burst shot): not a duplicate, must never be acted on automatically

# name -> expected outcome
ROBUSTNESS_VARIANTS = {
    "exact-copy": EXPECT_COPY,
    "exif-stripped": EXPECT_COPY,
    "lossless-png": EXPECT_COPY,
    "resized-50-q85": EXPECT_COPY,
    "resized-320-q70": EXPECT_COPY,
    "recompressed-q40": EXPECT_COPY,
    "rotated-tag": EXPECT_COPY,
    **{_orientation_variant(o): EXPECT_COPY for o in TRANSFORMS},
    "brightness-120": EXPECT_VARIANT,
    "crop-10": EXPECT_VARIANT,
    "crop-25": EXPECT_VARIANT,
    "burst-1s": EXPECT_DIFFERENT,
}


def _copy_exif(exif: Image.Exif) -> Image.Exif:
    result = Image.Exif()
    result.load(exif.tobytes())
    return result


def generate_variants(original: Path, target_dir: Path) -> Dict[str, Path]:
    """
    Generates the ROBUSTNESS_VARIANTS of an image (only for test data). Re-encoded copies drop the EXIF data
    (like messengers do), except the tagged rotation and the burst shot.

    :param original: JPEG with EXIF data
    :param target_dir: existing directory for the variants
    :return: variant name -> file
    """
    result = {}

    def path(name: str, suffix: str = ".jpg") -> Path:
        result[name] = target_dir / f"{original.stem}-{name}{suffix}"
        return result[name]

    shutil.copy2(original, path("exact-copy"))
    _strip_exif(original, path("exif-stripped"))
    with Image.open(original) as image:
        upright_exif = _copy_exif(image.getexif())
        upright = ImageOps.exif_transpose(image).convert("RGB")
    upright_exif.pop(EXIF_ORIENTATION, None)

    upright.save(path("lossless-png", ".png"))
    upright.resize((upright.width // 2, upright.height // 2), Image.Resampling.LANCZOS).save(
        path("resized-50-q85"), quality=85)
    small = upright.copy()
    small.thumbnail((320, 320), Image.Resampling.LANCZOS)
    small.save(path("resized-320-q70"), quality=70)
    upright.save(path("recompressed-q40"), quality=40)
    ImageEnhance.Brightness(upright).enhance(1.2).save(path("brightness-120"), quality=90)

    # pixels rotated by 90° counter-clockwise plus an orientation tag that rotates them back for display
    tagged_exif = _copy_exif(upright_exif)
    tagged_exif[EXIF_ORIENTATION] = 6
    upright.transpose(Image.Transpose.ROTATE_90).save(path("rotated-tag"), exif=tagged_exif, quality=90)

    for name, fraction in (("crop-10", 0.1), ("crop-25", 0.25)):
        # cut off on the left only, so the aspect ratio changes like in a typical manual crop
        upright.crop((int(upright.width * fraction), 0, upright.width, upright.height)).save(path(name), quality=90)

    for orientation in TRANSFORMS:
        orientation.apply_to_image(upright).save(path(_orientation_variant(orientation)), quality=90)

    # simulated burst shot: same camera, captured 1 s later, slightly different framing and light
    burst_exif = _copy_exif(upright_exif)
    exif_ifd = burst_exif.get_ifd(IFD_EXIF)
    captured = exif_ifd.get(TAG_DATETIME_ORIGINAL)
    if captured:
        shifted = datetime.strptime(str(captured)[:19], "%Y:%m:%d %H:%M:%S") + timedelta(seconds=1)
        exif_ifd[TAG_DATETIME_ORIGINAL] = shifted.strftime("%Y:%m:%d %H:%M:%S")
    burst = ImageEnhance.Brightness(upright.rotate(0.5, resample=Image.Resampling.BICUBIC)).enhance(1.02)
    burst.save(path("burst-1s"), exif=burst_exif, quality=90)
    return result


def _variant_of(path: str) -> str:
    stem = Path(path).stem
    return next((name for name in ROBUSTNESS_VARIANTS if stem.endswith(f"-{name}")), "original")


def _span(values: List[int]) -> str:
    return f"{min(values)}..{max(values)}" if min(values) != max(values) else str(values[0])


def _signature_timing(files: List[Path], repeats: int = 3) -> Dict[bool, float]:
    """
    :return: orientations computed -> mean ms per `compute_signature` call (single thread)
    """
    result = {}
    for orientations in (False, True):
        start = time.perf_counter()
        for _ in range(repeats):
            for file in files:
                compute_signature(file, orientations=orientations)
        result[orientations] = (time.perf_counter() - start) / (repeats * len(files)) * 1000
    return result


def robustness_benchmark(directory: Path, args):
    """
    Generates variants of every original in `directory` into a temporary directory and reports per variant
    whether it is grouped with its original, how the match is classified and whether it could be auto-acted on,
    with rotation/mirror matching off and on. Evaluated twice: on all files, and on the copies only (no edited
    variants or burst shots, which send their whole group to review).
    """
    originals = [f for f in find_image_files(directory)
                 if "-" not in f.stem and f.suffix.lower() in (".jpg", ".jpeg")]
    timing = _signature_timing(find_image_files(directory))
    print()
    print(f"Signature per file (single thread, {directory.name}): {timing[False]:.1f} ms upright only, "
          f"{timing[True]:.1f} ms with all 8 orientations (+{timing[True] / timing[False] - 1:.0%})")

    work_dir = Path(tempfile.mkdtemp(prefix="py-image-dedup-robustness-"))
    try:
        for original in originals:
            target = work_dir / original.stem
            target.mkdir()
            shutil.copy2(original, target / original.name)
            generate_variants(original, target)
        files = find_image_files(work_dir)

        def expectation(path: str) -> str:
            return ROBUSTNESS_VARIANTS.get(_variant_of(path), EXPECT_COPY)

        subsets = {
            "all files": [str(f) for f in files],
            "copies only": [str(f) for f in files if expectation(str(f)) == EXPECT_COPY],
        }
        print(f"Robustness: {len(originals)} originals, {len(files)} files (generated, deleted afterwards)")

        results = {}
        for match_rotations in (False, True):
            thresholds = replace(_thresholds(args), match_rotations=match_rotations)
            store = SignatureStore(":memory:")
            pipeline = MatchingPipeline(store, thresholds, workers=1, preview_size=args.preview_size,
                                        verify_pixel_identical=not args.no_verify)
            start = time.perf_counter()
            pipeline.analyse(files)
            analyse_ms = (time.perf_counter() - start) / len(files) * 1000
            label = f"rotations {'on' if match_rotations else 'off'}"
            print(f"  {label}: analyse {analyse_ms:.1f} ms/file (1 worker)")
            all_signatures = store.get_all()
            for subset, paths in subsets.items():
                wanted = set(paths)
                signatures = [s for s in all_signatures if s.path in wanted]
                expected = pairs_of_groups(
                    [p for p in group if expectation(p) != EXPECT_DIFFERENT]
                    for group in ground_truth_by_folder(paths))
                start = time.perf_counter()
                matches = find_matches(signatures, thresholds, pipeline._resolve_full_pixel_hash
                                       if pipeline.verify_pixel_identical else None)
                groups = cluster(signatures, matches, thresholds)
                match_ms = (time.perf_counter() - start) * 1000
                results[(match_rotations, subset)] = (signatures, matches, groups)
                found = pairs_of_groups(group.paths for group in groups)
                auto_found = pairs_of_groups(group.paths for group in groups if group.auto_act_allowed)
                auto_wrong = sum(1 for group in groups if group.auto_act_allowed
                                 and any(expectation(p) != EXPECT_COPY for p in group.paths))
                print(f"    {subset}: {len(paths)} files, {len(expected)} duplicate pairs, match {match_ms:.0f} ms, "
                      f"{len(groups)} groups, {sum(g.auto_act_allowed for g in groups)} auto-act "
                      f"({auto_wrong} with a variant or burst shot)")
                _print_metrics("    grouped pairs", evaluate_pairs(found, expected))
                _print_metrics("    auto-act pairs", evaluate_pairs(auto_found, expected))

        signatures_on, matches_on, groups_on = results[(True, "all files")]
        signatures = {s.path: s for s in signatures_on}
        matches = {(m.a, m.b): m for m in matches_on}
        thresholds = _thresholds(args)

        def group_index(groups) -> Dict[str, int]:
            return {path: index for index, group in enumerate(groups) for path in group.paths}

        index_on, index_off = group_index(groups_on), group_index(results[(False, "all files")][2])
        print()
        print("  Per variant, range over the originals (all files; distances to the original upright and in the "
              "best orientation; grouped with the original with rotations off/on; direct match with the "
              "original: kind, confidence, auto-act allowed for the pair):")
        print(f"  {'variant':<40} {'pHash':>7} {'dHash':>7} {'best p/d':>11} {'off':>4} {'on':>4}  match")
        for name, expected_outcome in ROBUSTNESS_VARIANTS.items():
            rows = []
            for original in originals:
                original_path = str(work_dir / original.stem / original.name)
                variant_path = next(p for p in signatures
                                    if Path(p).parent.name == original.stem and _variant_of(p) == name)
                a, b = signatures[original_path], signatures[variant_path]
                upright = (hamming_distance(a.phash, b.phash), hamming_distance(a.dhash, b.dhash))
                best = min([upright] + [(hamming_distance(p, b.phash), hamming_distance(d, b.dhash))
                                        for p, d in (a.orientation_hashes or ())], key=sum)
                found_off = original_path in index_off and index_off[original_path] == index_off.get(variant_path)
                found_on = original_path in index_on and index_on[original_path] == index_on.get(variant_path)
                rows.append((upright, best, found_off, found_on,
                             matches.get(tuple(sorted((original_path, variant_path))))))
            described = sorted({
                f"{m.kind.value} {m.confidence:.2f} "
                f"{'auto' if not m.kind.needs_review and m.confidence >= thresholds.auto_act_min_confidence else 'review'}"
                if m else "-" for *_, m in rows})
            print(f"  {f'{name} ({expected_outcome})':<40} {_span([r[0][0] for r in rows]):>7} "
                  f"{_span([r[0][1] for r in rows]):>7} "
                  f"{_span([r[1][0] for r in rows]) + '/' + _span([r[1][1] for r in rows]):>11} "
                  f"{sum(r[2] for r in rows)}/{len(rows):<2} {sum(r[3] for r in rows)}/{len(rows):<2}  "
                  f"{', '.join(described)}")
        print()
        print("  Groups (all files, rotations on):")
        print_groups(groups_on, work_dir)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def _random_bits(rng: random.Random, count: int) -> int:
    """
    :return: a 64 bit int with `count` distinct random bits set
    """
    return sum(1 << bit for bit in rng.sample(range(64), count))


def synthetic_benchmark(n: int, args):
    """
    Scaling test of the search and clustering with random hashes (no images needed):
    n hashes, 10% of them are near copies (a few flipped bits) of another hash.
    """
    print(f"Synthetic benchmark: {n} hashes")
    rng = random.Random(42)
    phashes, dhashes = [], []
    for i in range(n):
        if i and rng.random() < 0.1:
            source = rng.randrange(len(phashes))
            flip_p = _random_bits(rng, rng.randrange(4))
            flip_d = _random_bits(rng, rng.randrange(4))
            phashes.append(phashes[source] ^ flip_p)
            dhashes.append(dhashes[source] ^ flip_d)
        else:
            phashes.append(rng.getrandbits(64))
            dhashes.append(rng.getrandbits(64))

    tracemalloc.start()
    start = time.perf_counter()
    pairs = find_candidate_pairs(to_array(phashes), to_array(dhashes), args.max_phash, args.max_dhash)
    search_seconds = time.perf_counter() - start
    _, search_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    print(f"  candidate search: {search_seconds:.2f}s, {len(pairs)} pairs, traced peak {search_peak / 1e6:.1f} MB")

    signatures = [
        ImageSignature(path=f"/synthetic/{i:06d}.jpg", file_size=0, mtime_ns=0, sha256=f"sha{i}",
                       preview_pixel_hash=f"px{i}", phash=p, dhash=d, width=4000, height=3000, is_grayscale=False)
        for i, (p, d) in enumerate(zip(phashes, dhashes))
    ]
    for match_rotations in (False, True):
        if match_rotations:
            # random hashes for the other orientations (no rotated copies), measures the cost of the search
            signatures = [replace(s, orientation_hashes=tuple((rng.getrandbits(64), rng.getrandbits(64))
                                                              for _ in TRANSFORMS)) for s in signatures]
        thresholds = MatchingThresholds(max_phash_distance=args.max_phash, max_dhash_distance=args.max_dhash,
                                        match_rotations=match_rotations)
        start = time.perf_counter()
        matches = find_matches(signatures, thresholds)
        groups = cluster(signatures, matches, thresholds)
        print(f"  find_matches (incl. search) + cluster, rotations {'on' if match_rotations else 'off'}: "
              f"{time.perf_counter() - start:.2f}s, {len(matches)} matches, {len(groups)} groups, "
              f"max RSS {_max_rss_mb():.0f} MB")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("directory", nargs="?", type=Path, help="image directory (test data only!)")
    parser.add_argument("--db", default=":memory:", help="SQLite file for the signatures (default: in memory)")
    parser.add_argument("--workers", type=int, default=None, help="analysis threads (default: CPU count)")
    parser.add_argument("--preview-size", type=int, default=512, help="JPEG draft preview edge length")
    parser.add_argument("--no-verify", action="store_true", help="do not verify pixel-identical with a full decode")
    parser.add_argument("--max-phash", type=int, default=MatchingThresholds.max_phash_distance)
    parser.add_argument("--max-dhash", type=int, default=MatchingThresholds.max_dhash_distance)
    parser.add_argument("--burst-window", type=float, default=MatchingThresholds.burst_window_seconds)
    parser.add_argument("--no-rotations", action="store_true",
                        help="do not match rotated/mirrored copies (only the upright orientation)")
    parser.add_argument("--robustness", action="store_true",
                        help="generate and evaluate variants of the originals in the directory (temporary)")
    parser.add_argument("--ground-truth", choices=["folders"], default=None,
                        help="'folders': files in the same folder are the same photo (tests/images layout)")
    parser.add_argument("--compare-goldberg", action="store_true",
                        help="compare with pairwise image-match distances (no Elasticsearch needed)")
    parser.add_argument("--compare-elasticsearch", metavar="HOST:PORT", default=None,
                        help="compare with the production image-match + Elasticsearch backend")
    parser.add_argument("--image-match-max-distance", type=float, default=0.1,
                        help="image-match distance cutoff (production default: 0.1)")
    parser.add_argument("--elasticsearch-index", default="py-image-dedup-matching-benchmark")
    parser.add_argument("--synthetic", type=int, default=None, metavar="N",
                        help="scaling test of search + clustering with N random hashes")
    args = parser.parse_args()

    if args.synthetic:
        synthetic_benchmark(args.synthetic, args)
        if args.directory is None:
            return
    if args.directory is None:
        parser.error("directory is required unless --synthetic is given")

    directory = args.directory.resolve()
    paths, found, pipeline, groups = run_pipeline(directory, args)

    expected = None
    if args.ground_truth == "folders":
        expected = pairs_of_groups(ground_truth_by_folder(paths))
        print()
        print(f"Ground truth: {len(expected)} duplicate pairs")
        distance_sweep(paths, pipeline.store, expected)
        print("Pairwise precision/recall:")
        _print_metrics("matching pipeline (groups)", evaluate_pairs(found, expected))
        auto_found = pairs_of_groups(group.paths for group in groups if group.auto_act_allowed)
        _print_metrics("matching pipeline (auto-act groups only)", evaluate_pairs(auto_found, expected))

    if args.robustness:
        robustness_benchmark(directory, args)

    if args.compare_goldberg:
        goldberg_found, distances, seconds = goldberg_pairs(paths, args.image_match_max_distance)
        print()
        print(f"image-match (Goldberg, pairwise, cutoff {args.image_match_max_distance}): "
              f"signatures in {seconds:.2f}s (single thread)")
        if expected is not None:
            _print_metrics("image-match pairwise", evaluate_pairs(goldberg_found, expected))
            positives = [d for pair, d in distances.items() if pair in expected]
            negatives = [d for pair, d in distances.items() if pair not in expected]
            print(f"  distances: positives max {max(positives):.3f}, negatives min {min(negatives):.3f}")
            for pair in sorted(expected - goldberg_found):
                print(f"    missed: {_short(pair[0], directory)} ~ {_short(pair[1], directory)} "
                      f"distance {distances[pair]:.3f}")
        print(f"  pairs only found by the pipeline: {len(found - goldberg_found)}, "
              f"only by image-match: {len(goldberg_found - found)}")

    if args.compare_elasticsearch:
        es_found, seconds = elasticsearch_pairs(paths, args.compare_elasticsearch, args.image_match_max_distance,
                                                args.elasticsearch_index)
        print()
        print(f"image-match + Elasticsearch (cutoff {args.image_match_max_distance}): add + query in {seconds:.2f}s")
        if expected is not None:
            _print_metrics("image-match + Elasticsearch", evaluate_pairs(es_found, expected))
            for pair in sorted(expected - es_found):
                print(f"    missed: {_short(pair[0], directory)} ~ {_short(pair[1], directory)}")
        print(f"  pairs only found by the pipeline: {len(found - es_found)}, "
              f"only by image-match: {len(es_found - found)}")


if __name__ == "__main__":
    main()

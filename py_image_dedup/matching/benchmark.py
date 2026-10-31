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
"""
import argparse
import random
import resource
import time
import tracemalloc
from itertools import combinations
from pathlib import Path
from typing import Dict, List, Set, Tuple

from py_image_dedup.matching import (
    ImageSignature, MatchingPipeline, MatchingThresholds, SignatureStore, cluster, find_image_files, find_matches,
)
from py_image_dedup.matching.evaluation import (
    PairMetrics, evaluate_pairs, ground_truth_by_folder, pairs_of_groups,
)
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


def run_pipeline(directory: Path, args) -> Tuple[List[str], Set[Pair], MatchingPipeline]:
    thresholds = MatchingThresholds(
        max_phash_distance=args.max_phash,
        max_dhash_distance=args.max_dhash,
        burst_window_seconds=args.burst_window,
    )
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
    for group in result.groups:
        print(f"  [{group.reason.value}, confidence {group.confidence:.2f}] {len(group.paths)} files")
        for match in sorted(group.matches, key=lambda m: -m.confidence):
            notes = f" ({', '.join(match.notes)})" if match.notes else ""
            print(f"    {_short(match.a, directory)} ~ {_short(match.b, directory)}: {match.reason.value} "
                  f"{match.confidence:.2f} pHash {match.phash_distance} dHash {match.dhash_distance}{notes}")
        for conflict in group.conflicts:
            print(f"    CONFLICT (not joined): {_short(conflict.a, directory)} ~ {_short(conflict.b, directory)}")
    if result.vetoes:
        print("Vetoed candidate pairs:")
        for veto in result.vetoes:
            print(f"  {_short(veto.a, directory)} ~ {_short(veto.b, directory)}: {veto.reason}")

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
    return [str(f) for f in files], found, pipeline


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
    thresholds = MatchingThresholds(max_phash_distance=args.max_phash, max_dhash_distance=args.max_dhash)
    start = time.perf_counter()
    matches, _ = find_matches(signatures, thresholds)
    groups = cluster(signatures, matches, thresholds)
    print(f"  find_matches (incl. search) + cluster: {time.perf_counter() - start:.2f}s, {len(matches)} matches, "
          f"{len(groups)} groups, max RSS {_max_rss_mb():.0f} MB")


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
    paths, found, pipeline = run_pipeline(directory, args)

    expected = None
    if args.ground_truth == "folders":
        expected = pairs_of_groups(ground_truth_by_folder(paths))
        print()
        print(f"Ground truth: {len(expected)} duplicate pairs")
        distance_sweep(paths, pipeline.store, expected)
        print("Pairwise precision/recall:")
        _print_metrics("matching pipeline (groups)", evaluate_pairs(found, expected))

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

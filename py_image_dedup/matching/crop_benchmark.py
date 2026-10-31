"""
Benchmark of the opt-in crop detection stage (py_image_dedup.matching.embeddings), see the
"Crop detection (embeddings)" section of docs/matching-prototype.md.

Copies a test image directory (folder = photo, the tests/images layout) into a temporary directory, adds
generated crops of every original (not committed) and runs the matching pipeline with and without the
embedding stage. Reports pairwise precision/recall, the similarity distribution of crop pairs and negatives,
time per file, model size and memory.

Needs the optional dependencies (`poetry install -E embeddings`) and the model (downloaded on demand into the
cache directory, or `--model PATH`). Only run this on test data, it never modifies the input directory.

    poetry run python -m py_image_dedup.matching.crop_benchmark tests/images
"""
import argparse
import resource
import shutil
import tempfile
import time
from itertools import combinations
from pathlib import Path
from typing import Dict, List, Set, Tuple

import numpy as np
from PIL import Image, ImageOps

from py_image_dedup.matching import MatchingPipeline, MatchingThresholds, SqliteSignatureStore, find_image_files
from py_image_dedup.matching.embeddings import (
    DEFAULT_MIN_SIMILARITY, EmbeddingStage, EmbeddingStore, OnnxEmbeddingExtractor, ensure_model,
)
from py_image_dedup.matching.evaluation import evaluate_pairs, ground_truth_by_folder, pairs_of_groups

Pair = Tuple[str, str]

# name -> (left, top, right, bottom) as fractions of the width/height, keep_exif, max edge (resize) or None
CROPS: Dict[str, Tuple[Tuple[float, float, float, float], bool, int or None]] = {
    # remove 10 % / 25 % / 40 % of each edge length, same aspect ratio
    "crop10": ((0.05, 0.05, 0.95, 0.95), True, None),
    "crop25": ((0.10, 0.15, 0.85, 0.90), True, None),
    "crop40": ((0.20, 0.20, 0.80, 0.80), True, None),
    # other aspect ratios
    "crop-square": ((0.125, 0.0, 0.875, 1.0), True, None),
    "crop-16x9": ((0.0, 0.08, 1.0, 0.92), True, None),
    "crop-portrait": ((0.3, 0.0, 0.7, 1.0), True, None),
    # off-centre crop, metadata stripped and resized like a messenger copy
    "crop25-corner-telegram": ((0.0, 0.0, 0.75, 0.75), False, 1280),
}


def _max_rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def generate_dataset(source: Path, target: Path) -> List[Path]:
    """
    Copies `source` to `target` and adds the CROPS variants of the original of every folder (the file without
    a "-" suffix, or the first file).

    :return: generated files
    """
    shutil.copytree(source, target)
    generated = []
    for folder in sorted(p for p in target.iterdir() if p.is_dir()):
        files = sorted(find_image_files(folder))
        if not files:
            continue
        original = next((f for f in files if "-" not in f.stem), files[0])
        with Image.open(original) as image:
            exif = image.getexif()
            oriented = ImageOps.exif_transpose(image)
            if 0x0112 in exif:
                del exif[0x0112]
            width, height = oriented.size
            for name, ((left, top, right, bottom), keep_exif, max_edge) in CROPS.items():
                crop = oriented.crop((int(width * left), int(height * top), int(width * right), int(height * bottom)))
                if max_edge:
                    crop.thumbnail((max_edge, max_edge))
                path = folder / f"{original.stem}-{name}.jpg"
                crop.save(path, quality=88 if max_edge is None else 75, exif=exif if keep_exif else Image.Exif())
                generated.append(path)
    return generated


def run(directory: Path, embedding_stage: EmbeddingStage or None, thresholds: MatchingThresholds):
    pipeline = MatchingPipeline(SqliteSignatureStore(":memory:"), thresholds, embedding_stage=embedding_stage)
    files = find_image_files(directory)
    analysis = pipeline.analyse(files)
    start = time.perf_counter()
    result = pipeline.match()
    return files, analysis, result, time.perf_counter() - start


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("directory", type=Path, help="test image directory, folder = photo (e.g. tests/images)")
    parser.add_argument("--model", type=Path, default=None, help="ONNX model (default: download/cache SSCD)")
    parser.add_argument("--min-similarity", type=float, default=DEFAULT_MIN_SIMILARITY)
    parser.add_argument("--threads", type=int, default=None, help="onnxruntime threads (default: all cores)")
    parser.add_argument("--keep", type=Path, default=None, help="write the generated dataset here and keep it")
    args = parser.parse_args()

    model_path = args.model or ensure_model()
    rss_before = _max_rss_mb()
    start = time.perf_counter()
    extractor = OnnxEmbeddingExtractor(model_path, threads=args.threads)
    load_seconds = time.perf_counter() - start
    print(f"Model: {model_path} ({extractor.model_size_bytes / 1e6:.1f} MB, input {extractor.image_size} px), "
          f"loaded in {load_seconds:.2f}s, max RSS {rss_before:.0f} -> {_max_rss_mb():.0f} MB")

    temp_dir = Path(tempfile.mkdtemp(prefix="py-image-dedup-crops-"))
    try:
        directory = args.keep or temp_dir / "images"
        generated = generate_dataset(args.directory, directory)
        thresholds = MatchingThresholds()
        print(f"Dataset: {directory} ({len(generated)} generated crops)")

        files, _, baseline, _ = run(directory, None, thresholds)
        paths = [str(f) for f in files]
        expected = pairs_of_groups(ground_truth_by_folder(paths))
        crop_pairs = {p for p in expected if any("-crop" in Path(x).stem for x in p)}
        print(f"Files: {len(files)}, ground truth: {len(expected)} duplicate pairs ({len(crop_pairs)} with a crop)")

        stage = EmbeddingStage(extractor, EmbeddingStore(), min_similarity=args.min_similarity)
        start = time.perf_counter()
        embeddings = stage.update(_signatures(directory, thresholds))
        embed_seconds = time.perf_counter() - start
        print(f"Embeddings: {len(embeddings)} files in {embed_seconds:.2f}s "
              f"({embed_seconds / max(len(embeddings), 1) * 1000:.0f} ms/file wall incl. decode), "
              f"max RSS {_max_rss_mb():.0f} MB")

        _, _, with_embeddings, match_seconds = run(directory, stage, thresholds)
        print(f"Matching with embedding stage (embeddings cached): {match_seconds:.2f}s")

        print()
        print("Pairwise precision/recall:")
        for name, result in (("hashes only", baseline), ("hashes + embeddings", with_embeddings)):
            found = pairs_of_groups(group.paths for group in result.groups)
            metrics = evaluate_pairs(found, expected)
            crop_metrics = evaluate_pairs(found & crop_pairs, crop_pairs)
            print(f"  {name:<22} precision {metrics.precision:6.1%}  recall {metrics.recall:6.1%}  "
                  f"(tp {metrics.true_positives}, fp {metrics.false_positives}, fn {metrics.false_negatives}), "
                  f"crop pair recall {crop_metrics.recall:6.1%}")

        print()
        print("Cosine similarity:")
        _similarity_report(paths, embeddings, expected, crop_pairs)

        print()
        print("Groups with crops (review only, crop ranked first):")
        for group in with_embeddings.groups:
            for relation in group.crops:
                similarity = f"{relation.embedding_similarity:.2f}" if relation.embedding_similarity else "-"
                print(f"  {Path(relation.crop).name} crop of {Path(relation.source).name}: "
                      f"area {relation.area_ratio:.2f}, aspect differs {relation.aspect_ratio_differs}, "
                      f"similarity {similarity}")
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def _signatures(directory: Path, thresholds: MatchingThresholds):
    pipeline = MatchingPipeline(SqliteSignatureStore(":memory:"), thresholds)
    pipeline.analyse(find_image_files(directory))
    return pipeline.store.get_all()


def _similarity_report(paths: List[str], embeddings: Dict[str, np.ndarray], expected: Set[Pair],
                       crop_pairs: Set[Pair]):
    by_kind: Dict[str, List[Tuple[float, Pair]]] = {"crop pairs": [], "other duplicate pairs": [], "negatives": []}
    for pair in combinations(sorted(paths), 2):
        if pair[0] not in embeddings or pair[1] not in embeddings:
            continue
        similarity = float(embeddings[pair[0]] @ embeddings[pair[1]])
        kind = "crop pairs" if pair in crop_pairs else "other duplicate pairs" if pair in expected else "negatives"
        by_kind[kind].append((similarity, pair))
    for kind, values in by_kind.items():
        if not values:
            continue
        similarities = np.array([v for v, _ in values])
        print(f"  {kind:<22} n {len(values):4d}  min {similarities.min():.3f}  "
              f"p10 {np.percentile(similarities, 10):.3f}  median {np.median(similarities):.3f}  "
              f"max {similarities.max():.3f}")
    for similarity, (a, b) in sorted(by_kind["crop pairs"])[:5]:
        print(f"    lowest crop pair: {Path(a).name} ~ {Path(b).name}: {similarity:.3f}")
    for similarity, (a, b) in sorted(by_kind["negatives"], reverse=True)[:3]:
        print(f"    highest negative: {Path(a).name} ~ {Path(b).name}: {similarity:.3f}")


if __name__ == "__main__":
    main()

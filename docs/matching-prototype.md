# Matching pipeline prototype

Status: **experimental prototype**, not used by the deduplicator, the CLI or the daemon. Production still uses
`image-match` + Elasticsearch. This document summarises the design, the benchmark results on `tests/images/`
and the owner's decisions on the open questions. See the "Matching & ranking pipeline" section of `TODO.md` for the plan.

## What the prototype does

Package `py_image_dedup/matching/`:

| Module          | Purpose                                                                                                                                          |
|-----------------|--------------------------------------------------------------------------------------------------------------------------------------------------|
| `decoding.py`   | Pillow decode, JPEG draft mode (DCT scaling to >= 512 px), EXIF orientation applied (`ImageOps.exif_transpose`), conversion to RGB               |
| `metadata.py`   | `DateTimeOriginal` + `SubSecTimeOriginal`, camera make/model, `ImageUniqueID`, `Software`                                                        |
| `signatures.py` | SHA-256 of the file, SHA-256 of the decoded preview pixels, pHash + dHash (`imagehash`, 64 bit), grayscale flag, lazy full resolution pixel hash |
| `store.py`      | `SignatureStore` interface + `SqliteSignatureStore` (stdlib `sqlite3`, default), one row per file, incremental (size + mtime + `SIGNATURE_VERSION`), `open_signature_store(url)` factory |
| `store_postgres.py` | `PostgresSignatureStore` (psycopg 3, optional `postgres` extra), same record semantics                                                     |
| `search.py`     | All-pairs Hamming distance search, numpy (`bitwise_count` over `uint64`), block-wise to bound memory                                             |
| `grouping.py`   | Staged matching with reason + confidence, burst veto, constrained union-find clustering                                                          |
| `pipeline.py`   | `MatchingPipeline`: analyse (thread pool) into the store, match, cluster                                                                         |
| `evaluation.py` | Pairwise precision/recall against a ground truth                                                                                                 |
| `benchmark.py`  | Benchmark + comparison with image-match (`python -m py_image_dedup.matching.benchmark`)                                                          |
| `embeddings.py` | Opt-in crop detection: SSCD embeddings (onnxruntime), cosine similarity search, crop relations on groups, see "Crop detection" below           |
| `crop_benchmark.py` | Benchmark of the crop detection with generated crops (`python -m py_image_dedup.matching.crop_benchmark`)                                    |

The benchmark is run as a module (not a CLI command), only on test data:

```shell
poetry run python -m py_image_dedup.matching.benchmark tests/images --ground-truth folders --compare-goldberg
poetry run python -m py_image_dedup.matching.benchmark tests/images --ground-truth folders --compare-elasticsearch 127.0.0.1:9200
poetry run python -m py_image_dedup.matching.benchmark --synthetic 40000
```

### Stages

Every accepted pair gets the strongest reason that applies:

| Stage | Reason            | Criterion                                                                                                                                                                                                                                                                                                       | Confidence                                    |
|-------|-------------------|-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|-----------------------------------------------|
| 1     | `exact`           | same SHA-256 of the file                                                                                                                                                                                                                                                                                        | 1.00                                          |
| 2     | `pixel-identical` | same hash of the decoded pixels after applying the EXIF orientation. The cheap preview (draft) pixel hash is only a pre-filter, collisions are verified with a full decode. Near-duplicate candidates with hash distance 0 and the same dimensions are verified the same way (catches e.g. a lossless PNG of a JPEG) | 0.99                                          |
| 3     | `near-duplicate`  | pHash distance <= 10 **and** dHash distance <= 12, not vetoed                                                                                                                                                                                                                                                   | 0.50 .. 0.90 by distance, then adjusted, max 0.98 |

Near-duplicate confidence adjustments (notes are stored on the match, so the UI can explain them):

- `+0.08` same camera + same capture time (incl. sub-seconds), or same `ImageUniqueID`
- `-0.10` different `ImageUniqueID`
- `-0.15` aspect ratio differs by more than 2 % (crop)
- `-0.05` one copy is grayscale, the other is not

**Burst veto**: same camera (make + model) and both capture times present and `0 < |Δt| <= 10 s` means a
different shot of a series, never a duplicate. Copies keep the capture time (or lose it, e.g. Telegram strips
EXIF, then there is no veto). If only one file has sub-seconds, times are compared at second precision.

**Clustering**: union-find over all accepted matches, strongest first. A union is refused if it would put two
files into one group that are vetoed against each other, also indirectly (A~B, B~C, but A/C are burst shots:
B is a messenger copy without metadata that looks like both). Refused matches are kept as `conflicts` on the
group. Group confidence = the weakest match that was needed to connect the group ("weakest link").
Unlike the current per-reference-file candidate lists the result is transitive and independent of the
processing order.

## Results on `tests/images/`

Ground truth: each folder contains one photo in four versions (original, `-edited`, `-grayscale`,
`-telegram-compression` = 1280 px, no EXIF): 3 groups, 18 duplicate pairs, 48 non-duplicate pairs.

| Method                                                     | Precision | Recall | Missed                                    |
|------------------------------------------------------------|-----------|--------|-------------------------------------------|
| Prototype (pHash <= 10, dHash <= 12)                       | 100 %     | 100 %  | -                                         |
| image-match + Elasticsearch 7.17.28, cutoff 0.1 (default)  | 100 %     | 66.7 % | all 6 pairs with the `-edited` file of `building` and `clouds` (distance 0.158 .. 0.176) |
| image-match + Elasticsearch, cutoff 0.2                    | 100 %     | 100 %  | -                                         |

Distance separation (positives vs. closest negative):

| Signature            | Positives max | Negatives min |
|----------------------|---------------|---------------|
| pHash (of 64 bits)   | 8             | 24            |
| dHash (of 64 bits)   | 9             | 23            |
| image-match Goldberg | 0.176         | 0.589         |

All three separate the test set cleanly. The set is far too small (3 different photos, taken 29 s apart at most)
to calibrate thresholds for a real library, see the recommendations below.

### Robustness (generated variants, not committed)

Variants of the three originals generated with Pillow (range over the three images: pHash/dHash distance to
the original, prototype result, image-match pairwise distance):

| Variant                                                          | pHash  | dHash  | Prototype                 | image-match distance |
|------------------------------------------------------------------|--------|--------|---------------------------|----------------------|
| exact copy                                                       | 0      | 0      | exact                     | 0.000                |
| EXIF stripped (same JPEG scan data)                              | 0      | 0      | pixel-identical           | 0.000                |
| lossless PNG                                                     | 0      | 0      | pixel-identical           | 0.000                |
| resized 50 %, q85                                                | 0      | 0      | near-duplicate 0.90       | 0.034 .. 0.049       |
| resized to 320 px, q70                                           | 0 .. 2 | 0 .. 2 | near-duplicate 0.86..0.90 | 0.085 .. 0.102       |
| recompressed q40                                                 | 0 .. 2 | 0 .. 3 | near-duplicate 0.85..0.90 | 0.028 .. 0.040       |
| brightness +20 %                                                 | 0 .. 2 | 0 .. 1 | near-duplicate 0.85..0.88 | 0.049 .. 0.068       |
| rotated pixels + compensating orientation tag                    | 0      | 0      | near-duplicate 0.98       | 0.700 .. 0.754 (pairwise without orientation search) |
| crop 10 %                                                        | 8 .. 12 | 3 .. 14 | no match                | 0.232 .. 0.592       |
| crop 25 %                                                        | 18 .. 22 | 17 .. 22 | no match              | 0.397 .. 0.673       |
| rotated 90° (pixels, no tag)                                     | 30 .. 38 | 33 .. 35 | no match              | 0.700 .. 0.754       |
| mirrored                                                         | 28 .. 32 | 14 .. 38 | no match              | 0.430 .. 0.626       |
| simulated burst (+1 s, same camera, 0.5° rotation, +2 % brightness) | 2 .. 6 | 2 .. 3 | **vetoed**           | 0.088 .. 0.102 (**false positive** at cutoff 0.1 for 2 of 3) |

Takeaways:

- Messenger copies (resize + recompression) are trivially found by both. pHash is even more robust
  (a 320 px thumbnail at q70 is at distance 0..2, image-match is at the 0.1 cutoff).
- Neither method handles crops; the opt-in embedding stage does, see "Crop detection" below.
- Physically rotated/mirrored copies without an orientation tag are not found by the prototype. image-match
  finds rotated ones in production because `find_similar` searches all orientations. Hashing the 8 dihedral
  transforms (or only the 4 rotations) of each image would fix it for 8x hash storage and search time;
  real libraries rarely contain such copies since phones and Lightroom write the orientation tag.
- The burst veto is the main precision gain: near-identical series shots are exactly what image-match
  reports as duplicates today.

### Speed and memory

AMD Ryzen 7 5800X (16 threads), local SSD, 12 MP JPEGs from `tests/images/`, warm cache:

| Step                                                               | Time per file |
|--------------------------------------------------------------------|---------------|
| Prototype signature (SHA-256, draft decode 512, pixel hash, pHash, dHash, EXIF) | 30 ms  |
| same with draft 256                                                | 21 ms         |
| same without draft mode (full decode)                              | 161 ms        |
| full resolution pixel hash (only for preview hash collisions / distance 0 pairs) | 79 ms |
| image-match Goldberg signature                                     | 243 ms        |

Analysis is about **8x faster** than image-match per file. A 40k library needs ~20 min of CPU time on this
machine, ~2-3 min wall time with the thread pool (Pillow releases the GIL while decoding); on the NAS/k3s node reading the
files will dominate. No Elasticsearch round trips: image-match + ES needed 2.9 s to add and query the 12 test
images.

Search + clustering, synthetic hashes (`--synthetic`, 10 % near copies), single thread:

| Images  | Candidate search | Matching + clustering (incl. search) | Peak traced memory (search) | Max RSS |
|---------|------------------|---------------------------------------|-----------------------------|---------|
| 40,000  | 2.7 s            | 2.7 s                                 | 103 MB                      | 190 MB  |
| 100,000 | 18.7 s           | 19.1 s                                | 463 MB                      | 650 MB  |

The brute force search is O(n²) but fine for the target size (40k). Memory per block is
`256 x n x 8` bytes. An incremental daemon run (new files against the whole library, not implemented in the prototype) would be
O(n) per new file (well below 1 ms for 40k). Above ~100k images a BK-tree or multi-index hashing would be worth it.
The SQLite store needs roughly 0.5 KB per file including indexes (~20 MB for 40k files).

## Recommendations

1. **Replace image-match with this pipeline.** Same or better recall on the realistic cases (messenger copies,
   light edits, recompression), better precision thanks to the burst veto, 8x faster analysis, no
   Elasticsearch, deterministic and transitive groups.
2. **Thresholds**: start with pHash <= 10 and dHash <= 12 (both must hold), burst window 10 s.
   Before switching, run the benchmark on a sample of the real library (read only, e.g. one year of photos
   plus the Telegram folder) and label the groups in the 0.5..0.75 confidence range: that range decides where
   the auto-act threshold goes.
3. **Confidence-based action**: auto-act only on `exact`, `pixel-identical` and near-duplicates with
   confidence >= ~0.8 and no conflicts; everything else goes to the review queue of the web UI.
4. **Keep the "edited" and "grayscale" variants as matches, but let the prioritization rules decide.**
   The notes (`colour differs`, `aspect ratio differs`) should be exposed to the rules, e.g. "never delete a
   grayscale/cropped variant automatically".
5. Store the match reason/confidence in the existing state store (`candidates`, `matcher` column) so the web UI
   can show "why".
6. Keep the full resolution pixel hash lazy: it costs 79 ms/file and is only needed for a handful of pairs.

## Signature store backends

The pipeline only talks to the `SignatureStore` interface (`store.py`): upsert/get records keyed by path
(size, mtime, hashes, metadata), `get_all()`/`iter_all()` for the search, `get_file_states()` + `needs_analysis()`
for incremental runs, `remove()`/`remove_missing()` for deleted files, `set_full_pixel_hash()` for the lazy
pixel hash and `schema_version`. Stores are persistence only: the Hamming distance search always loads all
signatures and runs in numpy (`search.py`), so no database specific similarity search is needed.

`open_signature_store(url)` creates a store from a URL like setting (to be wired into `config.py` later):

| URL                                            | Store                                                             |
|------------------------------------------------|-------------------------------------------------------------------|
| `sqlite:///relative/signatures.db`             | SQLite, path relative to the working directory (SQLAlchemy style) |
| `sqlite:////data/signatures.db`                | SQLite, absolute path                                             |
| `/data/signatures.db` (no scheme)              | SQLite file                                                       |
| `sqlite://`, `sqlite:///:memory:`, `:memory:`  | SQLite in memory (tests)                                          |
| `postgresql://user:password@host:5432/db`      | Postgres (`postgres://` works too, any libpq URI incl. `?sslmode=...`) |

- **SQLite** (default) lives in its **own database file**, separate from the web UI state store
  (`py_image_dedup/state`), so it can be deleted and rebuilt independently (everything in it can be recomputed).
- **Postgres** needs the optional extra: `pip install py-image-dedup[postgres]` / `poetry install --extras postgres`
  (`psycopg[binary]` 3.x, imported lazily, the package works without it). Tables are prefixed with `matching_`
  (`matching_files`, `matching_meta`), so a database shared with other applications works; schema creation is
  serialised with an advisory lock. All signatures are streamed with a server side cursor.
- **Schema version**: `SCHEMA_VERSION` in `store.py` (SQLite `PRAGMA user_version`, Postgres `matching_meta`).
  The store is a cache, so a store with another schema version is dropped and rebuilt (full re-analysis).
  `SIGNATURE_VERSION` (`models.py`) triggers re-analysis of single records without touching the schema.
- Tests: `tests/test_matching_store.py` runs the same contract tests against SQLite (always) and Postgres
  (`integration` marker, `PY_IMAGE_DEDUP_TEST_POSTGRES_DSN`, Postgres service container in CI).

## Decisions (owner, 2026-10-31)

1. **Grayscale/edited versions** are variants to keep, unless the original does not look nice on its own; then
   the original can be deleted. That is a human decision: such groups go to review, never automatic action.
2. **Burst window**: keep a burst window as a decision factor; not a hard veto on any capture time difference.
3. **Rotated/mirrored copies without orientation tag**: yes, match them, even if it is expensive
   (4-8x hash storage and search time).
4. **Crops**: yes to AI embeddings for crops. The crop should win in most cases, but not all (review).
5. **HEIC/RAW**: possible, but not relevant to the owner (JPEG/PNG matter). Others may care, so support them
   later as optional extras.
6. **Migration**: run both backends side by side if easily possible and compare the results on the owner's dataset.
7. **Signature store**: a separate SQLite file for the hashes (not the state store database); if feasible also
   support Postgres (the owner runs a cluster instance). Done, see "Signature store backends".

## Crop detection (embeddings, opt-in)

Owner decision: catch crops with AI embeddings. Crops should win in most cases, but not all, so a crop and its
source always go to review, with the crop ranked first by default.

### How it works

- **Model**: [SSCD](https://github.com/facebookresearch/sscd-copy-detection) `sscd_disc_mixup` (Meta, MIT
  license, ResNet-50, 512 dimensional L2-normalised embeddings), a self-supervised model trained for copy
  detection (crops, rescaling, colour changes, overlays), run with `onnxruntime` on the CPU.
- **Optional dependency**: `poetry install -E embeddings` / `pip install 'py-image-dedup[embeddings]'`
  (`onnxruntime`, cp314 wheels for amd64 and arm64). `onnxruntime` is only imported when an
  `OnnxEmbeddingExtractor` is created; the matching package works without it. Not installed in the Docker image.
- **Model file**: not bundled in the repo or the image. Either
  - downloaded on demand by `ensure_model()` into `$PY_IMAGE_DEDUP_MODEL_CACHE` (default
    `~/.cache/py-image-dedup/models`), SHA-256 verified after the download and on every load. Default source: an
    ONNX export of the official TorchScript release, published on Hugging Face by a third party
    (`nutszebra/sscd_onnx`, pinned to revision `0f03b67`, sha256 `e65e2a5b...6f8`, 98 MB, fixed input
    1x3x320x320). Its embeddings were verified to be identical (max abs difference 1e-7) to an own export of
    the official file. That repository has no license tag; the weights themselves are MIT. Or
  - exported locally with `tools/export_sscd_onnx.py` (needs PyTorch in a throw-away virtualenv; downloads the
    official TorchScript file from `dl.fbaipublicfiles.com`, verifies its SHA-256, exports with dynamic batch
    and image size, checks onnxruntime against PyTorch) and passed by path. Recommended for production: no
    third-party binary.
- **Stage**: `EmbeddingStage` runs after the hash stages (`MatchingPipeline(..., embedding_stage=...)`, default
  off). It embeds every file (EXIF oriented JPEG draft decode, squashed to 320x320, ImageNet normalisation),
  caches the vectors in its own SQLite table (`EmbeddingStore`, invalidated by size, mtime and model id),
  searches all pairs by cosine similarity (numpy, block-wise) and adds pairs with similarity >= 0.6 that the
  hash stages did not match as `MatchReason.EMBEDDING` with the note `crop/embedding similarity 0.xx`. The
  burst veto applies. Confidence 0.50 .. 0.75 by similarity, i.e. never in the auto-act range.
- **Crop relation (ranking hint)**: after clustering, `annotate_crops` sets `DuplicateGroup.crops`
  (`CropRelation(crop, source, area_ratio, aspect_ratio_differs, embedding_similarity)`) and
  `DuplicateGroup.has_crops`. Pairwise "fewer pixels" cannot tell a crop from a downscaled copy (a 1280 px
  messenger copy has fewer pixels than a 10 % crop), so it is decided per group: the member with the most pixels
  is the reference; members connected to it by hash matches between images with the same (orientation free)
  aspect ratio show the full frame; all others (only connected through embedding matches or hash matches
  with another aspect ratio) are crops of the reference. This also runs without the embedding stage (hash
  matched crops with another aspect ratio). `rank_crops_first(paths, group.crops)` moves crops to the front;
  the prioritization should use it as the default, and a group with `has_crops` must never be acted on
  automatically (review only).

### Results (generated crops of `tests/images/`, not committed)

`python -m py_image_dedup.matching.crop_benchmark tests/images` copies the test images to a temporary
directory and adds 7 crops of every original: 10 %, 25 %, 40 % (of each edge, same aspect ratio), square, 16:9,
portrait (40 % of the width) and a 75 % corner crop resized to 1280 px without EXIF (messenger copy of a crop).
33 files, 165 duplicate pairs (147 involve a crop), 363 negative pairs.

| Method                         | Precision | Recall | Crop pair recall |
|--------------------------------|-----------|--------|------------------|
| hashes only                    | 100 %     | 21.8 % | 12.2 %           |
| hashes + embeddings (>= 0.6)   | 100 %     | 100 %  | 100 %            |

Cosine similarity:

| Pairs                                                         | min    | p10   | median | max   |
|---------------------------------------------------------------|--------|-------|--------|-------|
| with a crop (any two members of the same photo)               | 0.396  | 0.601 | 0.780  | 0.956 |
| other duplicates (edited, grayscale, telegram)                | 0.660  | 0.699 | 0.903  | 0.998 |
| negatives (different photos, same place, 29 s apart at most)  | -0.037 | 0.002 | 0.049  | 0.129 |

The best embedding match of every crop is between 0.73 (portrait crop) and 0.94. The lowest pairs (portrait
crop ~ edited copy, 0.40) are still grouped transitively. 19 of 21 crops were flagged as crops of the original
(`group.crops`); the 10 % crops of two photos were already matched by pHash/dHash with the same aspect ratio and
are therefore treated like a resized copy (the group is still found, but not flagged as containing a crop).

Cost (AMD Ryzen 7 5800X, 12 MP JPEGs):

| Item                                          | Value                                                                    |
|-----------------------------------------------|--------------------------------------------------------------------------|
| model file                                    | 98 MB (fp32)                                                             |
| model load                                    | 0.3 s, +240 MB RSS                                                       |
| embedding incl. decode, 1 onnxruntime thread  | 156 ms/file                                                              |
| embedding incl. decode, 16 threads            | 40 ms/file wall                                                          |
| peak RSS while embedding (batch 16)           | 560 .. 590 MB (default model, fixed batch 1), 1.3 GB (own export, dynamic batch) |
| embedding storage                             | 2 KB/file (512 x float32), 82 MB for 40k                                 |
| similarity search, brute force (numpy/BLAS)   | 40k: 6.6 s, 100k: 44 s; extra memory per block 256 x n x 4 bytes        |

A 40k library needs ~1.7 h of single core CPU time for the embeddings (once, cached afterwards), about 5x the
prototype hashes and less than image-match today. Brute force search is fine up to ~100k; beyond that, or for
incremental daemon runs (one new file against the library is O(n), ~1 ms for 40k), an ANN index (`sqlite-vec`,
FAISS) is the next step. float16 storage would halve the memory.

### Open points

- The threshold 0.6 is calibrated on 3 photos only. Real libraries contain similar but different photos (same
  scene, other people/cameras, so no burst veto) that may score higher; the low confidence keeps them in review,
  but the threshold should be checked on a real sample (read only).
- The 320x320 squash distorts crops with another aspect ratio; a dynamic size export
  (`tools/export_sscd_onnx.py`) could keep the aspect ratio (resize the short edge) and might raise the
  similarity of strong crops.
- Not wired into the deduplicator/daemon/config yet (options needed: enable flag, model path or URL + sha256,
  cache dir, min similarity); the Docker image would need the extra and a model volume.
- Light crops (~10 %) that are hash matched with the same aspect ratio are not flagged as crops.
- The embedding cache (`EmbeddingStore`) is a plain SQLite table (own file or in memory); it is not part of the
  `SignatureStore` interface, so there is no Postgres variant yet.

## Not done (intentionally)

- No wiring into `ImageMatchDeduplicator`, the daemon, CLI, config or Docker image.
- No `SignatureBackend` interface (image-match vs. hashes) yet: the prototype API (`MatchingPipeline.analyse/match`)
  should inform it. The store itself is abstracted (`SignatureStore`).
- Quality signals for prioritization (format tier, JPEG quality estimate, sharpness) are not part of this
  prototype.

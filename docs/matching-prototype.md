# Matching pipeline prototype

Status: **experimental prototype**, not used by the deduplicator, the CLI or the daemon. Production still uses
`image-match` + Elasticsearch. This document summarises the design, the benchmark results on `tests/images/`
and the decisions that are still open. See the "Matching & ranking pipeline" section of `TODO.md` for the plan.

## What the prototype does

Package `py_image_dedup/matching/`:

| Module          | Purpose                                                                                                                                          |
|-----------------|--------------------------------------------------------------------------------------------------------------------------------------------------|
| `decoding.py`   | Pillow decode, JPEG draft mode (DCT scaling to >= 512 px), EXIF orientation applied (`ImageOps.exif_transpose`), conversion to RGB               |
| `metadata.py`   | `DateTimeOriginal` + `SubSecTimeOriginal`, camera make/model, `ImageUniqueID`, `Software`                                                        |
| `signatures.py` | SHA-256 of the file, SHA-256 of the decoded preview pixels, pHash + dHash (`imagehash`, 64 bit), grayscale flag, lazy full resolution pixel hash |
| `store.py`      | `SignatureStore`: SQLite (stdlib `sqlite3`), one row per file, incremental (size + mtime + `SIGNATURE_VERSION`)                                  |
| `search.py`     | All-pairs Hamming distance search, numpy (`bitwise_count` over `uint64`), block-wise to bound memory                                             |
| `grouping.py`   | Staged matching with reason + confidence, burst veto, constrained union-find clustering                                                          |
| `pipeline.py`   | `MatchingPipeline`: analyse (thread pool) into the store, match, cluster                                                                         |
| `evaluation.py` | Pairwise precision/recall against a ground truth                                                                                                 |
| `benchmark.py`  | Benchmark + comparison with image-match (`python -m py_image_dedup.matching.benchmark`)                                                          |

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
- Neither method handles crops; that needs embeddings (SSCD, opt-in, see TODO).
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

## Open questions for the owner

1. **Are grayscale and edited versions duplicates you want removed**, or "variants" to keep next to the
   original? The test data puts them in the same group; the prototype matches them (with a note).
2. **Burst window**: 10 s is a guess. Should *any* different capture time of the same camera veto
   (strictest; risk: tools that shift capture times, e.g. time zone fixes, would then never match their source),
   or only a short window?
3. **Rotated/mirrored copies without orientation tag**: worth the 4-8x hash cost, or ignore?
4. **Crops**: is opt-in embedding search (SSCD + onnxruntime, ~100 MB model) wanted, or out of scope?
5. **HEIC/RAW**: are there HEIC (iPhone) or RAW files in the library? Then `pillow-heif` / `rawpy` previews
   are needed before switching (the prototype only handles what Pillow decodes).
6. **Migration**: switch hard (full re-analysis, remove Elasticsearch) or run both backends side by side for a
   while and compare the groups in the web UI (`matcher` column)?
7. Should the SQLite signature store live in the same database file as the state store (one PVC file) or in a
   separate one (can be deleted/rebuilt independently)?

## Not done (intentionally)

- No wiring into `ImageMatchDeduplicator`, the daemon, CLI, config or Docker image.
- No `SignatureBackend` interface yet: the prototype API (`MatchingPipeline.analyse/match`) should inform it.
- Quality signals for prioritization (format tier, JPEG quality estimate, sharpness) are not part of this
  prototype.

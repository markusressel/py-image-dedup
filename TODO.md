# TODO

## CI & dependencies

- [x] Refresh `poetry.lock`, fix Docker build on Python 3.14 via `scikit-image` 0.26.0 (#511)
- [x] Close superseded dependabot PRs (#507 tqdm, #508 urllib3, #509 numpy, #510 pillow)
- [ ] Sanity check major bumps in a real run: Pillow 13, python-dateutil 3.0, ruamel.yaml 0.19
- [x] Run unit tests (`poetry run pytest`) in CI on every PR (#513)
- [x] Integration test job with Elasticsearch service container, tested with ES 7.9.2 and 7.17.28 (#513)
- [x] `integration` pytest marker, excluded from the default run (#513)
- [x] Upgrade the cluster to Elasticsearch 7.17.x (tested in CI)
- [ ] Finish `tests/test_deduplicator.py::test_custom_prioritization` (expected order looks inverted:
      `smaller-distance` ranks `dist=0.1` first)
- [x] Slim down `Dockerfile` (2.95GB to 433MB), layer caching, fix entrypoint for unknown `PUID` (#514)
- [x] Fix outdated classifiers in `pyproject.toml` (#514)
- [x] Fix `make test` / `make docker` (#514)
- [x] Migrate `[tool.poetry]` metadata to `[project]` (PEP 621) (#528)
- [x] Use `httpx2` for starlette's TestClient (#531)

## Matching & ranking pipeline

Replaces `image-match` + Elasticsearch. Goal: group all copies of a photo with high precision, then let the
configurable prioritization rules pick the best one.

- [x] Prototype + benchmark: `py_image_dedup/matching/`, findings in `docs/matching-prototype.md` (#534)
- [x] Answer the open questions in `docs/matching-prototype.md` (variants, burst window, rotations, crops, HEIC/RAW,
      migration, store): see "Decisions" there
- [ ] Run the benchmark read-only on a real library sample, calibrate thresholds and the auto-act confidence
- [x] Signature store interface (`SignatureStore`, `open_signature_store(url)`), SQLite in its own file (default)
- [x] Optional Postgres signature store (`matching/store_postgres.py`, `postgres` extra, integration tests in CI)
- [ ] Wire the signature store URL into `config.py` (e.g. `matching.store: sqlite:////data/signatures.db`)
- [ ] Introduce a `SignatureBackend` interface so image-match and the hash pipeline can be swapped and compared
- [ ] Side-by-side comparison: `matching.backend: image-match|hashes|both`, compare the groups on the owner's
      dataset in the web UI (`matcher` column)
- [x] Rotation/mirror matching: hashes of all 8 dihedral orientations for copies without orientation tag,
      option `match_rotations` (prototype, ~7x search time)
- [x] Variants (grayscale, colour-edited) and crops: keep as matches, but always route to review instead of
      auto-acting: variant kind per match/member, `auto_act_allowed` + `review_reasons` per group (prototype)
- [x] Burst window as a decision factor (confidence penalty + note, group goes to review) instead of a hard veto
      (prototype, `burst_window_seconds`, `burst_confidence_penalty`)
- [ ] Expose variant kind, orientation and review reasons in the state store / web UI once the pipeline is wired in
- [ ] Crop detection with embeddings (SSCD via onnxruntime, opt-in); crops usually, but not always, win (review)
- [ ] SQLite store for hashes, metadata, groups, decisions and the action log (single file on a PVC)
  - [x] Groups, candidates, decisions (deciding rule), user overrides, runs and the action log:
        `py_image_dedup/state` (web UI backend, backend independent `matcher` column)
  - [x] Hashes and image metadata: `matching/store.py` (own SQLite file, separate from the state store),
        optional Postgres (`matching/store_postgres.py`)
- [ ] Decode & normalise: apply EXIF orientation before hashing (prototype done)
- [ ] HEIC (`pillow-heif`) and RAW previews (`rawpy`) as optional extras (not needed by the owner, later)
- [ ] Extract matching metadata: `DateTimeOriginal` (+ sub-seconds), camera model, `ImageUniqueID` /
      Apple content identifier, `Software` (edited) (prototype done, Apple content identifier missing)
- [ ] Staged grouping, each match tagged with confidence + reason:
  - [ ] Exact: file hash (SHA-256)
  - [ ] Pixel-identical: hash of decoded pixels
  - [ ] Near-duplicate: pHash + dHash (`imagehash`), Hamming distance search via numpy / BK-tree
  - [x] Optional, opt-in: embeddings (SSCD via onnxruntime) for cropped variants, crop and source go to
        review with the crop ranked first (prototype: `matching/embeddings.py`, `matching/crop_benchmark.py`)
  - [ ] Embeddings: wire into config/daemon (enable flag, model path, cache dir, min similarity), model volume
        in Docker/k3s, calibrate the similarity threshold on a real library sample
  - [ ] Embeddings: ANN index (`sqlite-vec` / FAISS) above ~100k images or for incremental daemon runs
- [x] Metadata boost: same capture time / unique ID boosts confidence (prototype)
- [ ] Cluster groups (connected components) instead of per-reference-file candidate lists
      (current matching is not transitive and depends on processing order)
- [ ] Additional quality signals for prioritization rules: format tier (RAW > lossless > HEIC > JPEG),
      estimated JPEG quality, sharpness (Laplacian variance), edited flag
- [ ] Confidence-based action: auto-act only on high confidence with a clear winner, otherwise queue for review
- [ ] Benchmark against the current `image-match` backend on `tests/images/` and a real library sample
- [ ] Migration: full re-analysis, new config options for thresholds, remove Elasticsearch from docs/compose/k3s

## Architecture & code quality

- [ ] Replace `DeduplicatorConfig` singleton with explicit config passing (testability)
- [x] Remove mutable class attributes (`_processed_files`, `queue`, `EXECUTOR`) shared across instances (#512)
- [x] Bounded Elasticsearch startup wait (`elasticsearch.connection_timeout`) instead of retrying forever (#512)
- [x] `ProcessingManager.remove()` modifies the queue without holding the lock (#530)
- [x] `_detect_db_version` logs a full traceback on every retry while ES boots (#530)
- [x] ES backend: on ES 7 documents are nested under `_doc` in `_source` (`KeyError: 'path'`, duplicate entries) (#517)
- [x] Groups whose files were removed outside of py-image-dedup stay open; failures of deleted files stay listed (#523)
- [x] Files deleted during a run are recorded in groups (#530)
- [x] File watcher dies on watchdog 6 `opened`/`closed` events (#530)
- [ ] Split `deduplicator.py` (>1000 lines): analysis, candidate search, selection/prioritization, file actions
- [ ] Introduce typed models instead of raw dicts keyed by `MetadataKey`
- [ ] Daemon: process individual files instead of whole parent directories (TODO in `processing_manager.py`)
- [ ] Replace `echo()`/console output with structured logging in daemon mode (#20)
- [ ] Use `ProcessPoolExecutor` for CPU-bound analysis (#83)
- [x] Watch newly created folders (#22, #530)
- [ ] Daemon: a folder with N changed files is processed N times
- [ ] Custom file filter (#417)

## Safety

- [ ] Default action "move to quarantine + review" instead of delete
- [x] Persist an action log (what was moved/deleted, why, which file was kept)
- [x] Undo actions from the action log (move files back) (#533)
- [x] `deduplication.protected_paths`: never touch curated folders (#527)
- [x] `dry_run_user_decisions`: execute user decisions while the rules stay in dry run (#524)
- [x] Flag duplicate groups with ambiguous ranking (decided by a weak tie-breaker rule) for manual review
- [x] Opt-in `deduplication.hold_ambiguous_groups`: never act on flagged groups automatically
- [ ] Enable `hold_ambiguous_groups` by default

## Web UI (`dedup.markusressel.de`, internal)

- [x] Choose stack: FastAPI (JSON API + static files, in the daemon process) + Vue 3/Vite/TypeScript SPA in `web/`
- [x] Backend: SQLite state store, recording in the deduplicator, JSON API under `/api`, thumbnails
- [x] Status page: current phase, progress, queue size, last run
- [x] History: past runs and actions taken
- [x] Review queue: duplicate groups with thumbnails, keep/move/ignore decisions
- [x] Insights: files failing analysis, rules that decided ties, config tuning hints
- [x] Docker: node build stage, one image serving API + SPA on port 8080; CI lints, type-checks and builds the SPA
- [ ] Live updates via SSE instead of polling (only if polling turns out to be too slow/heavy)
- [x] Bulk actions in the group list (accept/ignore/reset, also for all filtered groups) (#533)
- [x] Keep Prometheus metrics endpoint (stats port 8000 unchanged, also `/metrics` on the web port)
- [ ] Process individual files from the web UI (re-analyse, re-check a group)

## Deployment (k3s)

- [x] Rework manifests in `setup-manjaro/k3s/1_working/py-image-dedup` (separate ES 7.17.28, web UI ingress,
      probes, limits, state PVC)
- [ ] Commit the `setup-manjaro` changes, run ansible for the `dedup` DNS record
- [x] Multiarch Docker images (#77, #532)
- [ ] Pin image tag instead of `latest` + keel force policy
- [x] Deploy Immich in k3s as the Google Photos-like photo app (replacing the photo part of Plex),
      using the photo library as an external library; py-image-dedup stays the deduplication service

## Immich interop

- [x] Import the Immich trash (duplicate review) as user decisions (#524, #525)
- [x] Opt-in Immich library rescan after moves/deletes (#529)
- [x] "Open in Immich" links in the group detail (#529)
- [x] Preferred/avoided folder paths and edited copies as prioritization rules (#526, #527)
- [ ] Show Immich albums/faces in the group detail

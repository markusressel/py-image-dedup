# TODO

## CI & dependencies

- [ ] Merge `update-dependencies` (refreshed `poetry.lock`, fixes Docker build on Python 3.14 via `scikit-image` 0.26.0)
- [ ] Close superseded dependabot PRs (#507 tqdm, #508 urllib3, #509 numpy, #510 pillow)
- [ ] Sanity check major bumps in a real run: Pillow 13, python-dateutil 3.0, ruamel.yaml 0.19
- [ ] Run unit tests (`poetry run pytest`) in CI on every PR
- [ ] Add an integration test job: spin up Elasticsearch as a service container and run the deduplicator
      against `tests/images/` (`image-match` only works with specific Elasticsearch versions, this pins down which)
- [ ] Mark integration tests (e.g. `@pytest.mark.integration`) so they run against a real ES instead of the
      mocked backend in `tests.TestBase`
- [ ] Finish `tests/test_deduplicator.py::test_custom_prioritization` (expected order looks inverted:
      `smaller-distance` ranks `dist=0.1` first)
- [ ] Remove `python3-skimage` apt package from `Dockerfile` (system Python package, unused by the 3.14 interpreter)
- [ ] Fix outdated classifiers in `pyproject.toml` (lists Python 3.8/3.9, requires `^3.14`)
- [ ] Fix `make test` / `make docker` (`sudo`, `cd tests`) or replace with documented poetry commands

## Image signature & storage backend

- [ ] Decide: update the `image-match` fork vs. replace it
  - [ ] Option A: keep `image-match` + Elasticsearch, update fork, document supported ES versions
  - [ ] Option B: replace with `imagehash` (pHash/dHash) + SQLite/PostgreSQL, Hamming distance search
        (numpy / BK-tree), drop Elasticsearch entirely
- [ ] Benchmark both on `tests/images/` and a real library sample (accuracy, `max_distance` tuning, speed)
- [ ] Plan migration: full re-analysis, `DATAMODEL_VERSION` bump, config changes
- [ ] Make `ElasticSearchStoreBackend.__init__` fail with a timeout instead of retrying forever

## Architecture & code quality

- [ ] Replace `DeduplicatorConfig` singleton with explicit config passing (testability)
- [ ] Remove mutable class attributes (`_processed_files`, `queue`, `EXECUTOR`) shared across instances
- [ ] Split `deduplicator.py` (686 lines): analysis, candidate search, selection/prioritization, file actions
- [ ] Introduce typed models instead of raw dicts keyed by `MetadataKey`
- [ ] Daemon: process individual files instead of whole parent directories (TODO in `processing_manager.py`)
- [ ] Replace `echo()`/console output with structured logging in daemon mode (#20)
- [ ] Use `ProcessPoolExecutor` for CPU-bound analysis (#83)
- [ ] Watch newly created folders (#22)
- [ ] Custom file filter (#417)

## Safety

- [ ] Default action "move to quarantine + review" instead of delete
- [ ] Persist an action log (what was moved/deleted, why, which file was kept) to allow undo
- [ ] Never act on duplicate groups with ambiguous ranking; queue them for manual review instead

## Web UI (`dedup.markusressel.de`, internal)

- [ ] Choose stack (e.g. FastAPI + server-rendered templates/htmx, served by the daemon process)
- [ ] Status page: current phase, progress, queue size, last run
- [ ] History: past runs and actions taken
- [ ] Review queue: duplicate groups with thumbnails, keep/move/ignore decisions
- [ ] Insights: files failing analysis, rules that decided ties, config tuning hints
- [ ] Keep Prometheus metrics endpoint

## Deployment (k3s)

- [ ] Rework manifests in `setup-manjaro/k3s/1_working/py-image-dedup`:
  - [ ] Separate Elasticsearch from the app container (or drop it, see backend decision)
  - [ ] Pin Elasticsearch version tested by the integration test (currently 7.8.0 in cluster, 7.9.2 in compose)
  - [ ] Service + Ingress for the web UI on `dedup.markusressel.de` (internal only)
  - [ ] Liveness/readiness probes, resource limits
  - [ ] Clean up leftover Rancher/Helm labels and duplicate services
- [ ] Multiarch Docker images (#77)
- [ ] Pin image tag instead of `latest` + keel force policy

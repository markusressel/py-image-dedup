import datetime
import logging
import re
import tempfile
import time
from importlib import metadata as importlib_metadata
from pathlib import Path
from typing import List

from container_app_conf.source.env_source import EnvSource
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, generate_latest
from pydantic import BaseModel

from py_image_dedup.config import DeduplicatorConfig
from py_image_dedup.state.store import StateStore, GroupStatus
from py_image_dedup.web.thumbnails import ThumbnailCache

LOGGER = logging.getLogger(__name__)

# keep in sync with ImageMatchDeduplicator._quality_criteria and WEAK_PRIORITIZATION_RULES
KNOWN_PRIORITIZATION_RULES = {
    "more-exif-data", "less-exif-data",
    "bigger-file-size", "smaller-file-size",
    "newer-file-modification-date", "older-file-modification-date",
    "smaller-distance", "bigger-distance",
    "contains-copy-in-file-name", "doesnt-contain-copy-in-file-name",
    "longer-file-name", "shorter-file-name",
    "longer-folder-path", "shorter-folder-path",
    "higher-score", "lower-score",
    "higher-pixel-count", "lower-pixel-count",
}


class KeepRequest(BaseModel):
    candidate_id: int


def _version() -> str:
    try:
        return importlib_metadata.version("py-image-dedup")
    except Exception:
        return "unknown"


def _jsonable(value):
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime.timedelta):
        return str(value)
    if isinstance(value, re.Pattern):
        return value.pattern
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    return str(value)


def default_static_directory() -> Path:
    """
    :return: web/dist next to the py_image_dedup package (source checkout / Docker image)
    """
    return Path(__file__).resolve().parent.parent.parent / "web" / "dist"


def create_app(store: StateStore, config: DeduplicatorConfig, static_directory: Path | None = None,
               thumbnail_cache_directory: Path | None = None) -> FastAPI:
    """
    Creates the web UI application: a JSON API under /api and the single page app (if built).

    :param store: the state store to show
    :param config: the effective configuration
    :param static_directory: directory of the built single page app (index.html + assets)
    :param thumbnail_cache_directory: where to cache generated thumbnails
    """
    from py_image_dedup.library.deduplicator import WEAK_PRIORITIZATION_RULES

    app = FastAPI(title="py-image-dedup", version=_version(), docs_url="/api/docs", redoc_url=None,
                  openapi_url="/api/openapi.json")
    if thumbnail_cache_directory is None:
        thumbnail_cache_directory = Path(store.database_path).parent / "thumbnails" \
            if store.database_path != ":memory:" else Path(tempfile.gettempdir()) / "py-image-dedup-thumbnails"
    thumbnails = ThumbnailCache(thumbnail_cache_directory)

    def allowed_roots() -> List[Path]:
        roots = [Path(d).resolve() for d in config.SOURCE_DIRECTORIES.value]
        target = config.DEDUPLICATOR_DUPLICATES_TARGET_DIRECTORY.value
        if target is not None:
            roots.append(Path(target).resolve())
        return roots

    def is_allowed(path: Path) -> bool:
        try:
            resolved = path.resolve()
        except (OSError, RuntimeError):
            return False
        return any(resolved.is_relative_to(root) for root in allowed_roots())

    def move_target(path: str) -> str | None:
        target = config.DEDUPLICATOR_DUPLICATES_TARGET_DIRECTORY.value
        if target is None:
            return None
        return str(Path(str(target), *Path(path).parts[1:]))

    def candidate_file(candidate_id: int) -> Path:
        """
        :return: the file of a candidate: its original location or where it was moved to
        """
        candidate = store.get_candidate(candidate_id)
        if candidate is None:
            raise HTTPException(status_code=404, detail="Unknown candidate")
        path = Path(candidate["path"])
        if not path.is_file():
            moved_to = store.last_move_target(candidate["path"])
            if moved_to is None or not Path(moved_to).is_file():
                raise HTTPException(status_code=404, detail="File does not exist (anymore)")
            path = Path(moved_to)
        if not is_allowed(path):
            raise HTTPException(status_code=403, detail="File is outside of the configured directories")
        return path

    def group_or_404(group_id: int) -> dict:
        group = store.get_group(group_id)
        if group is None:
            raise HTTPException(status_code=404, detail="Unknown group")
        return group

    def with_details(group: dict) -> dict:
        """
        Adds file existence and the actions that will/would be executed to a group.
        """
        dry_run = config.DRY_RUN.value
        target_dir = config.DEDUPLICATOR_DUPLICATES_TARGET_DIRECTORY.value
        for candidate in group["candidates"]:
            candidate["exists"] = Path(candidate["path"]).is_file()
            candidate["moved_to"] = store.last_move_target(candidate["path"])
        planned = []
        if group["status"] in (GroupStatus.OPEN, GroupStatus.REVIEW):
            for candidate in group["candidates"]:
                if candidate["role"] == "keep" or not candidate["exists"]:
                    continue
                if target_dir is not None:
                    planned.append({"path": candidate["path"], "action": "move",
                                    "target": move_target(candidate["path"])})
                else:
                    planned.append({"path": candidate["path"], "action": "delete", "target": None})
        group["planned_actions"] = planned
        group["dry_run"] = dry_run
        group["actions"] = store.list_actions(limit=50, group_id=group["id"])["items"]
        return group

    # ------------------------------------------------------------------ status

    @app.get("/api/health")
    def health():
        return {"status": "ok"}

    @app.get("/api/status")
    def status():
        return {
            "version": _version(),
            "server_time": time.time(),
            "dry_run": config.DRY_RUN.value,
            "duplicates_target_directory": _jsonable(config.DEDUPLICATOR_DUPLICATES_TARGET_DIRECTORY.value),
            "hold_ambiguous_groups": config.DEDUPLICATION_HOLD_AMBIGUOUS_GROUPS.value,
            "live": store.live.snapshot(),
            "statistics": store.statistics(),
            "last_run": store.last_run(),
            "last_full_run": store.last_run("full"),
        }

    # ------------------------------------------------------------------ runs

    @app.get("/api/runs")
    def list_runs(limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0)):
        return {"items": store.list_runs(limit, offset)}

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: int):
        run = store.get_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="Unknown run")
        return run

    # ------------------------------------------------------------------ groups

    @app.get("/api/groups")
    def list_groups(status: str | None = Query(None), needs_review: bool | None = Query(None),
                    decided_by: str | None = Query(None), q: str | None = Query(None),
                    limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0)):
        if status is not None and status not in GroupStatus.ALL:
            raise HTTPException(status_code=400, detail=f"status must be one of {GroupStatus.ALL}")
        return store.list_groups(status=status, needs_review=needs_review, decided_by=decided_by, search=q,
                                 limit=limit, offset=offset)

    @app.get("/api/groups/{group_id}")
    def get_group(group_id: int):
        return with_details(group_or_404(group_id))

    @app.post("/api/groups/{group_id}/keep")
    def keep(group_id: int, request: KeepRequest):
        group = group_or_404(group_id)
        candidate = next((c for c in group["candidates"] if c["id"] == request.candidate_id), None)
        if candidate is None:
            raise HTTPException(status_code=400, detail="Candidate is not a member of this group")
        if config.DEDUPLICATOR_DUPLICATES_TARGET_DIRECTORY.value is None:
            raise HTTPException(status_code=409,
                                detail="duplicates_target_directory is not configured. Decisions from the web UI "
                                       "only move duplicates, they never delete files.")
        store.set_user_keeper(group_id, candidate["path"])
        return with_details(store.get_group(group_id))

    @app.post("/api/groups/{group_id}/ignore")
    def ignore(group_id: int):
        group_or_404(group_id)
        store.ignore_group(group_id)
        return with_details(store.get_group(group_id))

    @app.post("/api/groups/{group_id}/reset")
    def reset(group_id: int):
        group_or_404(group_id)
        store.reset_group(group_id)
        return with_details(store.get_group(group_id))

    # ------------------------------------------------------------------ files

    @app.get("/api/candidates/{candidate_id}/thumbnail")
    def thumbnail(candidate_id: int, size: int = Query(320, ge=16, le=4096)):
        path = candidate_file(candidate_id)
        try:
            thumbnail_path = thumbnails.get(path, size)
        except Exception as ex:
            LOGGER.warning(f"Could not create thumbnail for {path}: {ex}")
            raise HTTPException(status_code=422, detail="Could not create thumbnail")
        return FileResponse(thumbnail_path, media_type="image/jpeg",
                            headers={"Cache-Control": "private, max-age=3600"})

    @app.get("/api/candidates/{candidate_id}/image")
    def image(candidate_id: int):
        path = candidate_file(candidate_id)
        return FileResponse(path, headers={"Cache-Control": "private, max-age=3600"})

    # ------------------------------------------------------------------ actions & failures

    @app.get("/api/actions")
    def list_actions(limit: int = Query(100, ge=1, le=1000), offset: int = Query(0, ge=0),
                     group_id: int | None = None, run_id: int | None = None, status: str | None = None,
                     q: str | None = None):
        return store.list_actions(limit=limit, offset=offset, group_id=group_id, run_id=run_id, status=status,
                                  search=q)

    @app.get("/api/failures")
    def list_failures(limit: int = Query(100, ge=1, le=1000), offset: int = Query(0, ge=0)):
        return store.list_failures(limit, offset)

    # ------------------------------------------------------------------ config

    @app.get("/api/config")
    def get_config():
        entries = []
        for entry in config._config_entries.values():
            entries.append({
                "key": ".".join(entry.key_path),
                "env": EnvSource.env_key(entry),
                "value": "_REDACTED_" if entry.secret else _jsonable(entry.value),
                "default": _jsonable(entry.default),
                "description": entry.description,
            })
        entries.sort(key=lambda e: e["key"])
        return {"entries": entries, "hints": config_hints(), "weak_rules": sorted(WEAK_PRIORITIZATION_RULES)}

    def config_hints() -> List[dict]:
        hints = []
        statistics = store.statistics()

        if config.DRY_RUN.value:
            hints.append({"level": "info", "key": "dry_run",
                          "message": "Dry run is enabled: nothing on disk is moved or deleted, also not by "
                                     "decisions made in the web UI. The action log shows what WOULD happen."})
        if config.DEDUPLICATOR_DUPLICATES_TARGET_DIRECTORY.value is None:
            hints.append({"level": "warning", "key": "deduplication.duplicates_target_directory",
                          "message": "No duplicates_target_directory is set, so duplicates are DELETED when "
                                     "dry_run is disabled. Set it to move duplicates instead (recommended). "
                                     "Decisions from the web UI require it."})

        rule_names = [r.get("name") for r in config.PRIORITIZATION_RULES.value]
        unknown = [r for r in rule_names if r not in KNOWN_PRIORITIZATION_RULES]
        if unknown:
            hints.append({"level": "warning", "key": "deduplication.prioritization_rules",
                          "message": f"Unknown prioritization rules are ignored: {', '.join(map(str, unknown))}"})

        weak = {rule: n for rule, n in statistics["decided_by"].items() if rule in WEAK_PRIORITIZATION_RULES}
        weak_total = sum(weak.values())
        if weak_total > 0:
            details = ", ".join(f"{rule}: {n}" for rule, n in sorted(weak.items(), key=lambda x: -x[1]))
            message = f"{weak_total} duplicate group(s) were only decided by weak tie-breaker rules ({details}). " \
                      "Consider putting stronger rules (pixel count, EXIF data, file size) first in " \
                      "prioritization_rules, and review these groups."
            if not config.DEDUPLICATION_HOLD_AMBIGUOUS_GROUPS.value:
                message += " Enable deduplication.hold_ambiguous_groups to never act on them automatically."
            hints.append({"level": "warning", "key": "deduplication.prioritization_rules", "message": message,
                          "link": "/groups?needs_review=true"})

        if statistics["failures"] > 0:
            hints.append({"level": "warning", "key": "analysis",
                          "message": f"{statistics['failures']} file(s) failed analysis and are never deduplicated. "
                                     "They might be corrupt or use an unsupported format.",
                          "link": "/config#failures"})

        if len(config.SOURCE_DIRECTORIES.value) > 1 and not config.SEARCH_ACROSS_ROOT_DIRS.value:
            hints.append({"level": "info", "key": "analysis.across_dirs",
                          "message": "Multiple source directories are configured but across_dirs is disabled, "
                                     "duplicates in different source directories are not detected."})

        if config.MAX_FILE_MODIFICATION_TIME_DELTA.value is not None:
            hints.append({"level": "info", "key": "deduplication.max_file_modification_time_diff",
                          "message": "Copies whose modification time differs more than "
                                     f"{config.MAX_FILE_MODIFICATION_TIME_DELTA.value} from the best copy are kept."})
        return hints

    # ------------------------------------------------------------------ metrics

    @app.get("/metrics", include_in_schema=False)
    def metrics():
        # same metrics as the prometheus exporter on stats.port
        return Response(generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)

    # ------------------------------------------------------------------ single page app

    @app.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
    def api_not_found(path: str):
        return JSONResponse({"detail": "Not Found"}, status_code=404)

    static_root = Path(static_directory).resolve() if static_directory is not None else None
    if static_root is not None and (static_root / "index.html").is_file():
        index_file = static_root / "index.html"

        @app.get("/{path:path}", include_in_schema=False)
        def spa(path: str):
            if path:
                candidate = (static_root / path).resolve()
                if candidate.is_relative_to(static_root) and candidate.is_file():
                    # hashed asset file names can be cached forever
                    cache = "public, max-age=31536000, immutable" if path.startswith("assets/") else "no-cache"
                    return FileResponse(candidate, headers={"Cache-Control": cache})
            # client side routing: every other path is handled by the single page app
            return FileResponse(index_file, headers={"Cache-Control": "no-cache"})
    else:
        @app.get("/", include_in_schema=False)
        def no_ui():
            return HTMLResponse(
                "<!doctype html><title>py-image-dedup</title>"
                "<p>The web UI has not been built (<code>npm --prefix web run build</code>). "
                "The API is available under <a href='/api/docs'>/api/docs</a>.</p>")

    return app

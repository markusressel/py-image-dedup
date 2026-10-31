import hashlib
import json
import logging
import sqlite3
import time
from pathlib import Path
from threading import RLock
from typing import Iterable, List

from py_image_dedup.state.runtime_status import RuntimeStatus

LOGGER = logging.getLogger(__name__)

SCHEMA_VERSION = 1


class GroupStatus:
    OPEN = "open"  # decided by the prioritization rules (or the user), not (yet) executed for real
    REVIEW = "review"  # needs a human decision (e.g. only decided by a weak tie-breaker rule)
    RESOLVED = "resolved"  # duplicates have actually been moved/deleted
    IGNORED = "ignored"  # the user told us to never act on this group

    ALL = [OPEN, REVIEW, RESOLVED, IGNORED]


class DecisionSource:
    RULES = "rules"
    USER = "user"


class ActionStatus:
    DONE = "done"
    SIMULATED = "simulated"  # dry run: this is what WOULD have happened
    SKIPPED = "skipped"
    FAILED = "failed"


class BulkAction:
    ACCEPT = "accept"  # execute the decision of the prioritization rules (as a user decision)
    IGNORE = "ignore"
    RESET = "reset"

    ALL = [ACCEPT, IGNORE, RESET]


class RunStatus:
    RUNNING = "running"
    FINISHED = "finished"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


# a duplicate of the group has a higher resolution, or the same resolution and a bigger file, than the kept copy
_KEEPS_SMALLER_COPY_SQL = (
    "EXISTS (SELECT 1 FROM candidates k JOIN candidates d ON d.group_id = k.group_id "
    "WHERE k.group_id = g.id AND k.path = g.keep_path AND d.path != g.keep_path AND d.role != 'keep' "
    "AND (d.pixel_count > k.pixel_count OR (d.pixel_count = k.pixel_count AND d.file_size > k.file_size)))"
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    trigger TEXT,
    status TEXT NOT NULL,
    dry_run INTEGER NOT NULL,
    started_at REAL NOT NULL,
    finished_at REAL,
    error TEXT,
    summary TEXT
);
CREATE TABLE IF NOT EXISTS phases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    idx INTEGER,
    name TEXT NOT NULL,
    started_at REAL NOT NULL,
    finished_at REAL,
    done INTEGER,
    total INTEGER
);
CREATE INDEX IF NOT EXISTS idx_phases_run ON phases(run_id);
CREATE TABLE IF NOT EXISTS groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL UNIQUE,
    matcher TEXT NOT NULL,
    status TEXT NOT NULL,
    needs_review INTEGER NOT NULL DEFAULT 0,
    review_reason TEXT,
    decision_source TEXT NOT NULL,
    keep_path TEXT,
    rule_keep_path TEXT,
    decided_by TEXT,
    decision_reason TEXT,
    execute_requested INTEGER NOT NULL DEFAULT 0,
    candidate_count INTEGER NOT NULL DEFAULT 0,
    first_seen_run_id INTEGER,
    last_seen_run_id INTEGER,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    resolved_at REAL
);
CREATE INDEX IF NOT EXISTS idx_groups_status ON groups(status);
CREATE TABLE IF NOT EXISTS candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    path TEXT NOT NULL,
    rank INTEGER NOT NULL,
    role TEXT NOT NULL,
    keep_reason TEXT,
    distance REAL,
    score REAL,
    file_size INTEGER,
    pixel_count INTEGER,
    modified_at REAL,
    exif_tag_count INTEGER,
    metadata TEXT,
    UNIQUE (group_id, path)
);
CREATE INDEX IF NOT EXISTS idx_candidates_path ON candidates(path);
CREATE TABLE IF NOT EXISTS actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL NOT NULL,
    run_id INTEGER,
    group_id INTEGER,
    path TEXT NOT NULL,
    action TEXT NOT NULL,
    target TEXT,
    source TEXT NOT NULL,
    dry_run INTEGER NOT NULL,
    status TEXT NOT NULL,
    message TEXT
);
CREATE INDEX IF NOT EXISTS idx_actions_group ON actions(group_id);
CREATE INDEX IF NOT EXISTS idx_actions_path ON actions(path);
CREATE TABLE IF NOT EXISTS failures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL UNIQUE,
    phase TEXT,
    error TEXT,
    run_id INTEGER,
    count INTEGER NOT NULL DEFAULT 1,
    first_seen_at REAL NOT NULL,
    last_seen_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS restore_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action_id INTEGER NOT NULL UNIQUE REFERENCES actions(id) ON DELETE CASCADE,
    requested_at REAL NOT NULL
);
"""

# a successful (non dry run) move of a file that was not moved again or restored since
_UNDOABLE_MOVE_SQL = (
    "(a.action = 'move' AND a.status = 'done' AND a.dry_run = 0 AND a.target IS NOT NULL "
    "AND NOT EXISTS (SELECT 1 FROM actions b WHERE b.path = a.path AND b.id > a.id AND b.status = 'done' "
    "AND b.dry_run = 0 AND b.action IN ('move', 'restore')))"
)

_ACTION_COLUMNS_SQL = (
    f"a.*, {_UNDOABLE_MOVE_SQL} AS undoable, "
    "EXISTS (SELECT 1 FROM restore_requests r WHERE r.action_id = a.id) AS restore_requested"
)

CANDIDATE_FIELDS = ["distance", "score", "file_size", "pixel_count", "modified_at", "exif_tag_count"]


def group_key(paths: Iterable[str]) -> str:
    """
    Stable identity of a duplicate group, independent of the order in which its members were found.
    :param paths: file paths of all group members
    :return: hex digest
    """
    joined = "\n".join(sorted(set(str(p) for p in paths)))
    return hashlib.sha1(joined.encode("utf-8")).hexdigest()


class StateStore:
    """
    Small SQLite database holding the daemon state shown in the web UI:
    runs and their phases, duplicate groups with their candidates and the decision that was made,
    user overrides, the action log and files that failed analysis.

    Groups and decisions are modelled independently of the matching backend (``matcher`` column),
    so the planned matching & ranking pipeline can reuse this store.

    All methods are thread safe (a single connection guarded by a lock).
    """

    def __init__(self, database_path: str | Path):
        """
        :param database_path: path of the SQLite file (parent directories are created), or ":memory:"
        """
        self.database_path = str(database_path)
        if self.database_path != ":memory:":
            Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)

        self.live = RuntimeStatus()
        self._lock = RLock()
        self._conn = sqlite3.connect(self.database_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA foreign_keys = ON")
            if self.database_path != ":memory:":
                self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.executescript(_SCHEMA)
            self._conn.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', ?)",
                               (str(SCHEMA_VERSION),))
            # runs that were still "running" belong to a previous process that did not shut down cleanly
            self._conn.execute("UPDATE runs SET status = ?, finished_at = COALESCE(finished_at, started_at) "
                               "WHERE status = ?", (RunStatus.INTERRUPTED, RunStatus.RUNNING))
            self._conn.commit()
            self._failure_paths = set(r[0] for r in self._conn.execute("SELECT path FROM failures"))

    def close(self):
        with self._lock:
            self._conn.close()

    # ---------------------------------------------------------------- helpers

    def _execute(self, sql: str, params=()) -> sqlite3.Cursor:
        with self._lock, self._conn:
            return self._conn.execute(sql, params)

    def _query(self, sql: str, params=()) -> List[dict]:
        with self._lock:
            return [dict(row) for row in self._conn.execute(sql, params).fetchall()]

    def _query_one(self, sql: str, params=()) -> dict | None:
        rows = self._query(sql, params)
        return rows[0] if rows else None

    # ---------------------------------------------------------------- runs & phases

    def start_run(self, kind: str, dry_run: bool, trigger: str | None = None) -> int:
        """
        :param kind: "full" (all 6 phases) or "incremental" (daemon processing changed directories)
        :param dry_run: whether this run only simulates file actions
        :param trigger: optional description of what triggered the run (e.g. a directory)
        :return: run id
        """
        cursor = self._execute(
            "INSERT INTO runs (kind, trigger, status, dry_run, started_at) VALUES (?, ?, ?, ?, ?)",
            (kind, trigger, RunStatus.RUNNING, int(dry_run), time.time()))
        run_id = cursor.lastrowid
        self.live.run_started(run_id, kind)
        return run_id

    def finish_run(self, run_id: int, error: str | None = None, summary: dict | None = None):
        with self._lock, self._conn:
            self._conn.execute("UPDATE phases SET finished_at = ? WHERE run_id = ? AND finished_at IS NULL",
                               (time.time(), run_id))
            self._conn.execute(
                "UPDATE runs SET status = ?, finished_at = ?, error = ?, summary = ? WHERE id = ?",
                (RunStatus.FAILED if error else RunStatus.FINISHED, time.time(), error,
                 json.dumps(summary) if summary is not None else None, run_id))
        self.live.run_finished()

    def start_phase(self, run_id: int, name: str, index: int | None = None, count: int | None = None) -> int:
        """
        Starts a new phase of a run (finishing the previous one).
        """
        now = time.time()
        with self._lock, self._conn:
            self._conn.execute("UPDATE phases SET finished_at = ? WHERE run_id = ? AND finished_at IS NULL",
                               (now, run_id))
            cursor = self._conn.execute(
                "INSERT INTO phases (run_id, idx, name, started_at) VALUES (?, ?, ?, ?)",
                (run_id, index, name, now))
        self.live.phase_started(name, index, count)
        return cursor.lastrowid

    def update_phase_progress(self, run_id: int, done: int, total: int):
        """
        Adds the progress of a finished task to the currently running phase of a run.
        """
        self._execute(
            "UPDATE phases SET done = COALESCE(done, 0) + ?, total = COALESCE(total, 0) + ? "
            "WHERE run_id = ? AND finished_at IS NULL", (done, total, run_id))

    def list_runs(self, limit: int = 50, offset: int = 0) -> List[dict]:
        rows = self._query("SELECT * FROM runs ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset))
        return [self._run_row(r) for r in rows]

    def get_run(self, run_id: int) -> dict | None:
        row = self._query_one("SELECT * FROM runs WHERE id = ?", (run_id,))
        if row is None:
            return None
        run = self._run_row(row)
        run["phases"] = self._query("SELECT * FROM phases WHERE run_id = ? ORDER BY id", (run_id,))
        return run

    def last_run(self, kind: str | None = None) -> dict | None:
        if kind is None:
            row = self._query_one("SELECT * FROM runs ORDER BY id DESC LIMIT 1")
        else:
            row = self._query_one("SELECT * FROM runs WHERE kind = ? ORDER BY id DESC LIMIT 1", (kind,))
        return self._run_row(row) if row else None

    @staticmethod
    def _run_row(row: dict) -> dict:
        row = dict(row)
        row["dry_run"] = bool(row["dry_run"])
        row["summary"] = json.loads(row["summary"]) if row.get("summary") else None
        return row

    # ---------------------------------------------------------------- groups

    def record_group(self, run_id: int | None, matcher: str, candidates: List[dict], keep_paths: List[str],
                     decided_by: str | None, decision_reason: str | None,
                     needs_review: bool = False, review_reason: str | None = None) -> dict:
        """
        Records (or updates) a duplicate group and the decision the prioritization rules made for it.
        User decisions (ignore, chosen keeper) of an already known group are preserved.

        :param run_id: the run that found this group
        :param matcher: name of the matching backend that grouped the files (e.g. "image-match")
        :param candidates: all group members, ordered best first. Dicts with "path" and optionally
                           "role", "keep_reason" and the fields in CANDIDATE_FIELDS, extra data under "metadata"
        :param keep_paths: the files the rules decided to keep (first one is the best candidate)
        :param decided_by: name of the rule that decided between the best and the second best candidate
        :param decision_reason: human readable explanation of the decision
        :param needs_review: whether the decision is weak and should be reviewed by a human
        :param review_reason: why a review is needed
        :return: the stored group (without candidates)
        """
        paths = [str(c["path"]) for c in candidates]
        key = group_key(paths)
        now = time.time()
        rule_keep_path = str(keep_paths[0]) if keep_paths else None

        with self._lock, self._conn:
            existing = self._conn.execute("SELECT * FROM groups WHERE key = ?", (key,)).fetchone()
            if existing is None:
                status = GroupStatus.REVIEW if needs_review else GroupStatus.OPEN
                cursor = self._conn.execute(
                    "INSERT INTO groups (key, matcher, status, needs_review, review_reason, decision_source, "
                    "keep_path, rule_keep_path, decided_by, decision_reason, candidate_count, first_seen_run_id, "
                    "last_seen_run_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (key, matcher, status, int(needs_review), review_reason, DecisionSource.RULES, rule_keep_path,
                     rule_keep_path, decided_by, decision_reason, len(paths), run_id, run_id, now, now))
                group_id = cursor.lastrowid
            else:
                existing = dict(existing)
                group_id = existing["id"]
                decision_source = existing["decision_source"]
                keep_path = existing["keep_path"]
                if decision_source != DecisionSource.USER or keep_path not in paths:
                    decision_source = DecisionSource.RULES
                    keep_path = rule_keep_path

                status = existing["status"]
                if status != GroupStatus.IGNORED and not (
                    decision_source == DecisionSource.USER and status == GroupStatus.OPEN):
                    # the duplicates (still or again) exist, so the group is not resolved
                    status = GroupStatus.REVIEW if needs_review and decision_source == DecisionSource.RULES \
                        else GroupStatus.OPEN

                self._conn.execute(
                    "UPDATE groups SET matcher = ?, status = ?, needs_review = ?, review_reason = ?, "
                    "decision_source = ?, keep_path = ?, rule_keep_path = ?, decided_by = ?, decision_reason = ?, "
                    "candidate_count = ?, last_seen_run_id = ?, updated_at = ?, resolved_at = NULL WHERE id = ?",
                    (matcher, status, int(needs_review), review_reason, decision_source, keep_path, rule_keep_path,
                     decided_by, decision_reason, len(paths), run_id, now, group_id))

            # upsert, so candidate ids (used in URLs, e.g. thumbnails) stay stable across runs
            keep_paths = [str(p) for p in keep_paths]
            for rank, candidate in enumerate(candidates):
                path = str(candidate["path"])
                role = candidate.get("role") or ("keep" if path in keep_paths else "duplicate")
                self._conn.execute(
                    "INSERT INTO candidates (group_id, path, rank, role, keep_reason, distance, score, file_size, "
                    "pixel_count, modified_at, exif_tag_count, metadata) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(group_id, path) DO UPDATE SET rank = excluded.rank, role = excluded.role, "
                    "keep_reason = excluded.keep_reason, distance = excluded.distance, score = excluded.score, "
                    "file_size = excluded.file_size, pixel_count = excluded.pixel_count, "
                    "modified_at = excluded.modified_at, exif_tag_count = excluded.exif_tag_count, "
                    "metadata = excluded.metadata",
                    (group_id, path, rank, role, candidate.get("keep_reason"),
                     *[candidate.get(f) for f in CANDIDATE_FIELDS],
                     json.dumps(candidate.get("metadata") or {}, default=str)))

            if existing is not None and decision_source == DecisionSource.USER:
                self._update_roles(group_id, keep_path)

        return self.get_group(group_id, with_candidates=False)

    def get_group(self, group_id: int, with_candidates: bool = True) -> dict | None:
        row = self._query_one("SELECT * FROM groups WHERE id = ?", (group_id,))
        if row is None:
            return None
        group = self._group_row(row)
        if with_candidates:
            group["candidates"] = self.get_candidates(group_id)
        return group

    def get_candidates(self, group_id: int) -> List[dict]:
        rows = self._query("SELECT * FROM candidates WHERE group_id = ? ORDER BY rank", (group_id,))
        for row in rows:
            row["metadata"] = json.loads(row["metadata"]) if row["metadata"] else {}
        return rows

    def get_candidate(self, candidate_id: int) -> dict | None:
        row = self._query_one("SELECT * FROM candidates WHERE id = ?", (candidate_id,))
        if row is not None:
            row["metadata"] = json.loads(row["metadata"]) if row["metadata"] else {}
        return row

    def list_groups(self, status: str | None = None, needs_review: bool | None = None, search: str | None = None,
                    decided_by: str | None = None, keeps_smaller_copy: bool | None = None,
                    limit: int = 50, offset: int = 0) -> dict:
        """
        Lists groups, groups that need a human decision first, then most recently updated first.
        :param keeps_smaller_copy: only groups where a duplicate has a higher resolution or a bigger file
        :return: {"total": int, "items": [group, ...]}
        """
        where_sql, params = self._group_filter(status=status, needs_review=needs_review, search=search,
                                               decided_by=decided_by, keeps_smaller_copy=keeps_smaller_copy)

        total = self._query_one(f"SELECT COUNT(*) AS n FROM groups g {where_sql}", params)["n"]
        rows = self._query(
            f"SELECT g.* FROM groups g {where_sql} "
            "ORDER BY CASE g.status WHEN 'review' THEN 0 WHEN 'open' THEN 1 WHEN 'resolved' THEN 2 ELSE 3 END, "
            "g.execute_requested DESC, g.updated_at DESC, g.id DESC LIMIT ? OFFSET ?",
            params + [limit, offset])
        items = [self._group_row(r) for r in rows]
        if items:
            # attach a light-weight preview of the members (for thumbnails in list views)
            ids = [g["id"] for g in items]
            placeholders = ",".join("?" * len(ids))
            members = self._query(
                f"SELECT id, group_id, path, role, rank FROM candidates WHERE group_id IN ({placeholders}) "
                "ORDER BY group_id, rank", ids)
            by_group = {}
            for m in members:
                by_group.setdefault(m["group_id"], []).append(m)
            for g in items:
                g["members"] = by_group.get(g["id"], [])
        return {"total": total, "items": items}

    def list_group_ids(self, status: str | None = None, needs_review: bool | None = None, search: str | None = None,
                       decided_by: str | None = None, keeps_smaller_copy: bool | None = None) -> List[int]:
        """
        :return: ids of all groups matching the given filter (same filter as list_groups)
        """
        where_sql, params = self._group_filter(status=status, needs_review=needs_review, search=search,
                                               decided_by=decided_by, keeps_smaller_copy=keeps_smaller_copy)
        return [row["id"] for row in self._query(f"SELECT g.id FROM groups g {where_sql} ORDER BY g.id", params)]

    @staticmethod
    def _group_filter(status: str | None = None, needs_review: bool | None = None, search: str | None = None,
                      decided_by: str | None = None, keeps_smaller_copy: bool | None = None) -> tuple:
        """
        :return: (WHERE clause for the groups table aliased as "g", parameters)
        """
        where, params = [], []
        if keeps_smaller_copy:
            where.append(_KEEPS_SMALLER_COPY_SQL)
        if status:
            where.append("g.status = ?")
            params.append(status)
        if needs_review is not None:
            where.append("g.needs_review = ?")
            params.append(int(needs_review))
        if decided_by:
            where.append("g.decided_by = ?")
            params.append(decided_by)
        if search:
            where.append("EXISTS (SELECT 1 FROM candidates c WHERE c.group_id = g.id AND c.path LIKE ? ESCAPE '\\')")
            escaped = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            params.append(f"%{escaped}%")
        where_sql = ("WHERE " + " AND ".join(where)) if where else ""
        return where_sql, params

    def bulk_decision(self, group_ids: Iterable[int], action: str) -> dict:
        """
        Applies the same decision to many groups. Only groups the decision makes sense for are changed:

        * accept: open/review groups decided by the rules: the rule keeper becomes the user chosen keeper,
          so the daemon executes it (like set_user_keeper). Ignored groups and groups with a decision
          of the user are not changed (reset them first).
        * ignore: open/review groups
        * reset: open/review/ignored groups with a user decision or the ignored status

        Resolved groups are never changed.

        :param group_ids: the groups to change
        :param action: one of BulkAction.ALL
        :return: {"action": str, "changed": [group id, ...], "skipped": {reason: count}}
        :raises ValueError: unknown action
        """
        if action not in BulkAction.ALL:
            raise ValueError(f"action must be one of {BulkAction.ALL}")
        changed, skipped = [], {}

        def skip(reason: str):
            skipped[reason] = skipped.get(reason, 0) + 1

        with self._lock:
            for group_id in dict.fromkeys(group_ids):
                group = self.get_group(group_id, with_candidates=False)
                if group is None:
                    skip("unknown")
                elif group["status"] == GroupStatus.RESOLVED:
                    skip("resolved")
                elif action == BulkAction.ACCEPT:
                    if group["status"] == GroupStatus.IGNORED:
                        skip("ignored")
                    elif group["decision_source"] == DecisionSource.USER:
                        skip("decided_by_user")
                    elif not group["rule_keep_path"]:
                        skip("no_rule_decision")
                    else:
                        self.set_user_keeper(group_id, group["rule_keep_path"])
                        changed.append(group_id)
                elif action == BulkAction.IGNORE:
                    if group["status"] == GroupStatus.IGNORED:
                        skip("already_ignored")
                    else:
                        self.ignore_group(group_id)
                        changed.append(group_id)
                elif group["status"] != GroupStatus.IGNORED and group["decision_source"] != DecisionSource.USER:
                    skip("nothing_to_reset")
                else:
                    self.reset_group(group_id)
                    changed.append(group_id)
        return {"action": action, "changed": changed, "skipped": skipped}

    def set_user_keeper(self, group_id: int, keep_path: str) -> dict:
        """
        The user chose which file of a group to keep. The daemon executes this decision
        (see pending_executions()), respecting dry_run.
        :raises KeyError: unknown group
        :raises ValueError: keep_path is not a member of the group
        """
        group = self.get_group(group_id)
        if group is None:
            raise KeyError(group_id)
        if keep_path not in [c["path"] for c in group["candidates"]]:
            raise ValueError(f"'{keep_path}' is not a member of group {group_id}")
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE groups SET decision_source = ?, keep_path = ?, status = ?, execute_requested = 1, "
                "updated_at = ? WHERE id = ?",
                (DecisionSource.USER, keep_path, GroupStatus.OPEN, time.time(), group_id))
            self._update_roles(group_id, keep_path)
        return self.get_group(group_id)

    def ignore_group(self, group_id: int) -> dict:
        """
        Never act on this group (until reset).
        """
        self._require_group(group_id)
        self._execute("UPDATE groups SET status = ?, execute_requested = 0, updated_at = ? WHERE id = ?",
                      (GroupStatus.IGNORED, time.time(), group_id))
        return self.get_group(group_id)

    def reset_group(self, group_id: int) -> dict:
        """
        Drops user decisions (ignore, chosen keeper) and goes back to the decision of the prioritization rules.
        """
        group = self._require_group(group_id)
        status = GroupStatus.REVIEW if group["needs_review"] else GroupStatus.OPEN
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE groups SET decision_source = ?, keep_path = rule_keep_path, status = ?, "
                "execute_requested = 0, updated_at = ? WHERE id = ?",
                (DecisionSource.RULES, status, time.time(), group_id))
            if group["rule_keep_path"]:
                self._update_roles(group_id, group["rule_keep_path"])
        return self.get_group(group_id)

    def import_discarded_paths(self, discarded_paths: Iterable[str], apply: bool = False) -> dict:
        """
        Applies decisions made in another tool (f.ex. the duplicate review of Immich) to the open groups:
        if all but one member of a group were discarded there, the remaining one becomes the user chosen keeper.
        Groups with an existing user decision, without discarded members, with all members discarded or with
        more than one remaining member are left alone.

        :param discarded_paths: paths of the files that were discarded (as seen by this application)
        :param apply: False only reports what would be decided
        :return: number of groups per outcome and the decided groups ({"group_id", "keep_path"})
        """
        discarded = set(str(p) for p in discarded_paths)
        outcomes = {"decided": 0, "no_discarded_member": 0, "all_discarded": 0, "several_remaining": 0,
                    "already_decided_by_user": 0}
        decided = []
        rows = self._query("SELECT id, decision_source FROM groups WHERE status IN (?, ?) ORDER BY id",
                           (GroupStatus.OPEN, GroupStatus.REVIEW))
        for row in rows:
            paths = [c["path"] for c in self.get_candidates(row["id"])]
            remaining = [p for p in paths if p not in discarded]
            if len(remaining) == len(paths):
                outcome = "no_discarded_member"
            elif row["decision_source"] == DecisionSource.USER:
                outcome = "already_decided_by_user"
            elif not remaining:
                outcome = "all_discarded"
            elif len(remaining) > 1:
                outcome = "several_remaining"
            else:
                outcome = "decided"
                decided.append({"group_id": row["id"], "keep_path": remaining[0]})
            outcomes[outcome] += 1

        if apply:
            for decision in decided:
                self.set_user_keeper(decision["group_id"], decision["keep_path"])
        return {"applied": apply, "outcomes": outcomes, "decided": decided}

    def pending_executions(self) -> List[dict]:
        """
        :return: groups with a user decision the daemon still has to execute
        """
        rows = self._query("SELECT * FROM groups WHERE execute_requested = 1 AND status != ? ORDER BY updated_at",
                           (GroupStatus.IGNORED,))
        result = []
        for row in rows:
            group = self._group_row(row)
            group["candidates"] = self.get_candidates(group["id"])
            result.append(group)
        return result

    def mark_executed(self, group_id: int, resolved: bool):
        """
        :param group_id: the group whose user decision was executed
        :param resolved: True if the duplicates are actually gone (not a dry run, no failures)
        """
        now = time.time()
        if resolved:
            self._execute("UPDATE groups SET execute_requested = 0, status = ?, resolved_at = ?, updated_at = ? "
                          "WHERE id = ?", (GroupStatus.RESOLVED, now, now, group_id))
        else:
            self._execute("UPDATE groups SET execute_requested = 0, updated_at = ? WHERE id = ?", (now, group_id))

    def mark_resolved(self, group_ids: Iterable[int]):
        now = time.time()
        with self._lock, self._conn:
            for group_id in group_ids:
                self._conn.execute(
                    "UPDATE groups SET status = ?, resolved_at = ?, updated_at = ? WHERE id = ? AND status != ?",
                    (GroupStatus.RESOLVED, now, now, group_id, GroupStatus.IGNORED))

    def resolve_unseen_groups(self, run_id: int) -> int:
        """
        Marks open groups that were not found again by the given (full) run as resolved,
        f.ex. because some of their files have been removed by another tool in the meantime.
        Only call this after a full run, which sees every file in the source directories.

        :param run_id: the full run
        :return: number of resolved groups
        """
        now = time.time()
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "UPDATE groups SET status = ?, resolved_at = ?, updated_at = ? "
                "WHERE status IN (?, ?) AND (last_seen_run_id IS NULL OR last_seen_run_id != ?)",
                (GroupStatus.RESOLVED, now, now, GroupStatus.OPEN, GroupStatus.REVIEW, run_id))
            return cursor.rowcount

    def mark_restored(self, group_id: int):
        """
        A moved file of this group was moved back to its original location, so the group is not resolved anymore.
        It is ignored: neither the prioritization rules nor a pending user decision move the file again
        (until the user resets the group).
        """
        self._execute("UPDATE groups SET status = ?, execute_requested = 0, resolved_at = NULL, updated_at = ? "
                      "WHERE id = ?", (GroupStatus.IGNORED, time.time(), group_id))

    def group_ids_of_path(self, path: str) -> List[int]:
        """
        :return: ids of all groups the given file is a member of
        """
        return [row["group_id"] for row in
                self._query("SELECT DISTINCT group_id FROM candidates WHERE path = ?", (str(path),))]

    def _require_group(self, group_id: int) -> dict:
        group = self.get_group(group_id, with_candidates=False)
        if group is None:
            raise KeyError(group_id)
        return group

    def _update_roles(self, group_id: int, keep_path: str):
        self._conn.execute(
            "UPDATE candidates SET role = CASE WHEN path = ? THEN 'keep' ELSE 'duplicate' END WHERE group_id = ?",
            (keep_path, group_id))

    @staticmethod
    def _group_row(row: dict) -> dict:
        row = dict(row)
        row["needs_review"] = bool(row["needs_review"])
        row["execute_requested"] = bool(row["execute_requested"])
        return row

    # ---------------------------------------------------------------- actions

    def record_action(self, path: str, action: str, source: str, dry_run: bool, status: str,
                      target: str | None = None, message: str | None = None,
                      run_id: int | None = None, group_id: int | None = None) -> int:
        cursor = self._execute(
            "INSERT INTO actions (created_at, run_id, group_id, path, action, target, source, dry_run, status, "
            "message) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (time.time(), run_id, group_id, str(path), action, str(target) if target else None, source,
             int(dry_run), status, message))
        return cursor.lastrowid

    def list_actions(self, limit: int = 100, offset: int = 0, group_id: int | None = None,
                     run_id: int | None = None, status: str | None = None, search: str | None = None) -> dict:
        """
        :return: {"total": int, "items": [action, ...]}, newest first. Every action has "undoable"
                 (see request_restore) and "restore_requested".
        """
        where, params = [], []
        if group_id is not None:
            where.append("a.group_id = ?")
            params.append(group_id)
        if run_id is not None:
            where.append("a.run_id = ?")
            params.append(run_id)
        if status:
            where.append("a.status = ?")
            params.append(status)
        if search:
            where.append("a.path LIKE ? ESCAPE '\\'")
            escaped = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            params.append(f"%{escaped}%")
        where_sql = ("WHERE " + " AND ".join(where)) if where else ""
        total = self._query_one(f"SELECT COUNT(*) AS n FROM actions a {where_sql}", params)["n"]
        rows = self._query(f"SELECT {_ACTION_COLUMNS_SQL} FROM actions a {where_sql} ORDER BY a.id DESC "
                           "LIMIT ? OFFSET ?", params + [limit, offset])
        return {"total": total, "items": [self._action_row(r) for r in rows]}

    def get_action(self, action_id: int) -> dict | None:
        row = self._query_one(f"SELECT {_ACTION_COLUMNS_SQL} FROM actions a WHERE a.id = ?", (action_id,))
        return self._action_row(row) if row is not None else None

    @staticmethod
    def _action_row(row: dict) -> dict:
        for key in ["dry_run", "undoable", "restore_requested"]:
            row[key] = bool(row[key])
        return row

    def undoable_moves_of_group(self, group_id: int) -> List[dict]:
        """
        :return: the moves of the files of this group that can be undone (see request_restore)
        """
        rows = self._query(
            f"SELECT {_ACTION_COLUMNS_SQL} FROM actions a "
            f"WHERE a.path IN (SELECT path FROM candidates WHERE group_id = ?) AND {_UNDOABLE_MOVE_SQL} ORDER BY a.id",
            (group_id,))
        return [self._action_row(r) for r in rows]

    def request_restore(self, action_id: int) -> dict:
        """
        Requests to move a moved file back to its original location. The daemon executes it
        (see pending_restores()), respecting dry_run.

        :param action_id: the last successful (non dry run) move of a file
        :return: the action
        :raises KeyError: unknown action
        :raises ValueError: the action can not be undone
        """
        action = self.get_action(action_id)
        if action is None:
            raise KeyError(action_id)
        if not action["undoable"]:
            raise ValueError("Only the last successful move of a file (not a dry run) can be undone")
        self._execute("INSERT OR IGNORE INTO restore_requests (action_id, requested_at) VALUES (?, ?)",
                      (action_id, time.time()))
        return self.get_action(action_id)

    def pending_restores(self) -> List[dict]:
        """
        :return: the move actions the daemon has to undo, oldest request first
        """
        rows = self._query(f"SELECT {_ACTION_COLUMNS_SQL} FROM restore_requests r0 "
                           "JOIN actions a ON a.id = r0.action_id ORDER BY r0.id")
        return [self._action_row(r) for r in rows]

    def finish_restore(self, action_id: int):
        """
        Removes the restore request of a move action (after it was executed, skipped or failed).
        """
        self._execute("DELETE FROM restore_requests WHERE action_id = ?", (action_id,))

    def last_move_target(self, path: str) -> str | None:
        """
        :return: where a file was moved to by a successful (non dry run) move action, if any
                 and the file was not restored to its original location since
        """
        row = self._query_one(
            "SELECT action, target FROM actions WHERE path = ? AND action IN ('move', 'restore') AND status = ? "
            "AND dry_run = 0 ORDER BY id DESC LIMIT 1", (str(path), ActionStatus.DONE))
        return row["target"] if row is not None and row["action"] == "move" else None

    # ---------------------------------------------------------------- failures

    def record_failure(self, path: str, error: str, phase: str | None = None, run_id: int | None = None):
        now = time.time()
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO failures (path, phase, error, run_id, first_seen_at, last_seen_at) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(path) DO UPDATE SET phase = excluded.phase, "
                "error = excluded.error, run_id = excluded.run_id, count = count + 1, "
                "last_seen_at = excluded.last_seen_at",
                (str(path), phase, error, run_id, now, now))
            self._failure_paths.add(str(path))

    def clear_failure(self, path: str):
        """
        Forgets a previous failure of a file (cheap no-op if the file never failed).
        """
        path = str(path)
        if path not in self._failure_paths:
            return
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM failures WHERE path = ?", (path,))
            self._failure_paths.discard(path)

    def failure_paths(self) -> List[str]:
        """
        :return: paths of all files with a recorded failure
        """
        return [row["path"] for row in self._query("SELECT path FROM failures")]

    def list_failures(self, limit: int = 100, offset: int = 0) -> dict:
        total = self._query_one("SELECT COUNT(*) AS n FROM failures")["n"]
        rows = self._query("SELECT * FROM failures ORDER BY last_seen_at DESC LIMIT ? OFFSET ?", (limit, offset))
        return {"total": total, "items": rows}

    # ---------------------------------------------------------------- statistics

    def statistics(self) -> dict:
        groups = {s: 0 for s in GroupStatus.ALL}
        for row in self._query("SELECT status, COUNT(*) AS n FROM groups GROUP BY status"):
            groups[row["status"]] = row["n"]
        actions = {}
        for row in self._query("SELECT action, status, COUNT(*) AS n FROM actions GROUP BY action, status"):
            actions.setdefault(row["action"], {})[row["status"]] = row["n"]
        decided_by = {
            (row["decided_by"] or "unknown"): row["n"] for row in
            self._query("SELECT decided_by, COUNT(*) AS n FROM groups WHERE status != ? GROUP BY decided_by",
                        (GroupStatus.IGNORED,))
        }
        return {
            "groups": groups,
            "groups_total": sum(groups.values()),
            "needs_review": self._query_one(
                "SELECT COUNT(*) AS n FROM groups WHERE needs_review = 1 AND status IN (?, ?)",
                (GroupStatus.REVIEW, GroupStatus.OPEN))["n"],
            "execute_requested": self._query_one(
                "SELECT COUNT(*) AS n FROM groups WHERE execute_requested = 1")["n"],
            "restore_requested": self._query_one("SELECT COUNT(*) AS n FROM restore_requests")["n"],
            "duplicate_files_open": self._query_one(
                "SELECT COUNT(*) AS n FROM candidates c JOIN groups g ON g.id = c.group_id "
                "WHERE c.role != 'keep' AND g.status IN (?, ?)", (GroupStatus.OPEN, GroupStatus.REVIEW))["n"],
            "actions": actions,
            "failures": self._query_one("SELECT COUNT(*) AS n FROM failures")["n"],
            "decided_by": decided_by,
            "runs": self._query_one("SELECT COUNT(*) AS n FROM runs")["n"],
            "keeps_smaller_copy": self._query_one(
                f"SELECT COUNT(*) AS n FROM groups g WHERE g.status IN (?, ?) AND {_KEEPS_SMALLER_COPY_SQL}",
                (GroupStatus.OPEN, GroupStatus.REVIEW))["n"],
        }

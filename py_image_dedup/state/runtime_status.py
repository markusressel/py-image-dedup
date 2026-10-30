import time
from threading import Lock


class RuntimeStatus:
    """
    In-memory snapshot of what the daemon is doing right now.

    Progress updates arrive once per processed file, so they are kept in memory only
    (no database write per file). Completed phases are persisted by the StateStore.
    """

    def __init__(self):
        self._lock = Lock()
        self._state = {
            "busy": False,
            "run_id": None,
            "run_kind": None,
            "phase": None,
            "phase_index": None,
            "phase_count": None,
            "task": None,
            "done": 0,
            "total": 0,
            "current_item": None,
            "queue_size": 0,
            "started_at": None,
            "updated_at": time.time(),
        }

    def run_started(self, run_id: int, kind: str):
        with self._lock:
            self._state.update(
                busy=True, run_id=run_id, run_kind=kind, phase=None, phase_index=None, phase_count=None,
                task=None, done=0, total=0, current_item=None, started_at=time.time(), updated_at=time.time(),
            )

    def run_finished(self):
        with self._lock:
            self._state.update(
                busy=False, phase=None, phase_index=None, phase_count=None, task=None,
                done=0, total=0, current_item=None, updated_at=time.time(),
            )

    def phase_started(self, name: str, index: int | None, count: int | None):
        with self._lock:
            self._state.update(phase=name, phase_index=index, phase_count=count, task=None, done=0, total=0,
                               current_item=None, updated_at=time.time())

    def task_started(self, task: str, total: int):
        with self._lock:
            self._state.update(task=task, done=0, total=total, current_item=None, updated_at=time.time())

    def task_progress(self, done: int, current_item: str | None = None):
        with self._lock:
            self._state["done"] = done
            if current_item is not None:
                self._state["current_item"] = current_item
            self._state["updated_at"] = time.time()

    def set_current_item(self, item: str):
        with self._lock:
            self._state["current_item"] = item

    def task_finished(self):
        with self._lock:
            self._state.update(task=None, current_item=None, updated_at=time.time())

    def set_queue_size(self, size: int):
        with self._lock:
            self._state["queue_size"] = size

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self._state)

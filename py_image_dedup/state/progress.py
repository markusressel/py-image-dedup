from typing import Callable


class StateProgressListener:
    """
    Forwards ProgressManager updates to the live status of a StateStore,
    and adds the progress of finished tasks to the current phase of the current run.
    """

    def __init__(self, store, run_id_provider: Callable[[], int | None]):
        """
        :param store: the StateStore
        :param run_id_provider: returns the id of the currently active run (or None)
        """
        self._store = store
        self._run_id_provider = run_id_provider

    def task_started(self, task: str, total: int):
        self._store.live.task_started(task, total)

    def task_item(self, item: str):
        self._store.live.set_current_item(item)

    def task_progress(self, done: int):
        self._store.live.task_progress(done)

    def task_finished(self, done: int, total: int):
        self._store.live.task_finished()
        run_id = self._run_id_provider()
        if run_id is not None:
            self._store.update_phase_progress(run_id, done, total)

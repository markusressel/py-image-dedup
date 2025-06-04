import logging

from tqdm import tqdm

LOGGER = logging.getLogger(__name__)
LOGGER.setLevel(logging.DEBUG)


class ProgressManager:

    def __init__(self):
        self._progress_bar = None
        self._task = None
        self._n = None
        self._total = None
        self._unit = None
        self._last_percentage = None

    def start(self, task: str, total: int, unit: str, interactive: bool):
        if self._task is not None:
            LOGGER.warning(f"Starting new progress without explicitly closing the current one '{self._task}'")
            self.clear()

        self._task = task
        self._total = total
        self._unit = unit
        if interactive:
            self._progress_bar = self._create_progressbar(total, unit)

    def set_postfix(self, postfix: str):
        if self._progress_bar is not None and postfix is not None:
            self._progress_bar.set_postfix_str(postfix)

    def inc(self, n: int = 1):
        if self._task is None:
            raise AssertionError(
                "Can't increase before start. "
                "Please start a new task progress using start() before incrementing it.")

        if self._n is None:
            self._n = n
        else:
            self._n += n

        if self._progress_bar is not None:
            self._progress_bar.update(n)

        new_percentage = int((self._n / self._total) * 100)
        if self._last_percentage is None or self._last_percentage != new_percentage:
            self._last_percentage = new_percentage
            LOGGER.info(f"{self._task}: {new_percentage}% ({self._n}/{self._total})")

    def clear(self):
        if self._progress_bar is not None:
            self._progress_bar.close()
            self._progress_bar = None
        self._last_percentage = None
        self._n = None
        self._total = None
        self._task = None
        self._unit = None

    def _create_progressbar(self, total_count: int, unit: str) -> tqdm:
        """
        Creates a new progress bar
        :param total_count: target for 100%
        :param unit: "Things" that are counted
        :return: progress bar
        """
        self._progress_bar = tqdm(total=total_count, unit=unit, unit_scale=True, mininterval=1)
        return self._progress_bar

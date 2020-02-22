import logging
import threading

LOGGER = logging.getLogger(__name__)
LOGGER.setLevel(logging.DEBUG)


class Action:
    def __init__(self, name, color, ):
        self.name = name
        self.color = color


class ActionEnum:
    NONE = Action("-", "green")
    DELETE = Action("delete", "red")
    MOVE = Action("move", "yellow")


class RegularIntervalWorker:
    """
    Base class for a worker that executes a specific task in a regular interval.
    """

    def __init__(self, interval: float):
        self._interval = interval
        self._timer = None

    def start(self):
        """
        Starts the worker
        """
        if self._timer is None:
            LOGGER.debug(f"Starting worker: {self.__class__.__name__}")
            self._schedule_next_run()
        else:
            LOGGER.debug("Already running, ignoring start() call")

    def stop(self):
        """
        Stops the worker
        """
        if self._timer is not None:
            self._timer.cancel()
        self._timer = None

    def _schedule_next_run(self):
        """
        Schedules the next run
        """
        if self._timer is not None:
            self._timer.cancel()
        self._timer = threading.Timer(self._interval, self._worker_job)
        self._timer.start()

    def _worker_job(self):
        """
        The regularly executed task. Override this method.
        """
        try:
            self._run()
        except Exception as e:
            LOGGER.error(e, exc_info=True)
        finally:
            self._schedule_next_run()

    def _run(self):
        """
        The regularly executed task. Override this method.
        """
        raise NotImplementedError()

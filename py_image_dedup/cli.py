from pathlib import Path
from typing import List

import click
from watchdog.observers.inotify import InotifyObserver

from py_image_dedup.config import DeduplicatorConfig
from py_image_dedup.library.deduplicator import ImageMatchDeduplicator
from py_image_dedup.library.file_watch import EventHandler
from py_image_dedup.library.processing import ProcessingManager
from py_image_dedup.util import echo

IMAGE_HASH_MAP = {}

PARAM_SKIP_ANALYSE_PHASE = "skip-analyse-phase"
PARAM_DRY_RUN = "dry-run"

CMD_OPTION_NAMES = {
    PARAM_SKIP_ANALYSE_PHASE: ['--skip-analyse-phase', '-sap'],
    PARAM_DRY_RUN: ['--dry-run', '-dr']
}

CONTEXT_SETTINGS = dict(help_option_names=['-h', '--help'])


@click.group(context_settings=CONTEXT_SETTINGS)
@click.version_option()
def cli():
    pass


def get_option_names(parameter: str) -> list:
    """
    Returns a list of all valid console parameter names for a given parameter
    :param parameter: the parameter to check
    :return: a list of all valid names to use this parameter
    """
    return CMD_OPTION_NAMES[parameter]


@cli.command(name="analyse")
def c_analyse():
    deduplicator = ImageMatchDeduplicator()
    deduplicator.analyse_all()


@cli.command(name="deduplicate")
@click.option(*get_option_names(PARAM_SKIP_ANALYSE_PHASE), required=False, default=False, is_flag=True,
              help='When set the image analysis phase will be skipped. Useful if you already did a dry-run.')
@click.option(*get_option_names(PARAM_DRY_RUN), required=False, default=None, is_flag=True,
              help='When set no files or folders will actually be deleted but a preview of '
                   'what WOULD be done will be printed.')
def c_deduplicate(skip_analyse_phase: bool,
                  dry_run: bool):
    config = DeduplicatorConfig()
    if dry_run is not None:
        config.DRY_RUN.value = dry_run
    deduplicator = ImageMatchDeduplicator()
    result = deduplicator.deduplicate_all(
        skip_analyze_phase=skip_analyse_phase,
    )

    echo()
    result.print_to_console()


def _setup_file_observers(source_directories: List[Path], event_handler):
    observers = []

    for directory in source_directories:
        # observer = PollingObserver()
        observer = InotifyObserver()
        observer.schedule(event_handler, str(directory), recursive=True)
        observer.start()
        observers.append(observer)

    return observers


@cli.command(name="daemon")
def c_daemon():
    config = DeduplicatorConfig()

    deduplicator = ImageMatchDeduplicator()
    processing_manager = ProcessingManager(deduplicator)
    event_handler = EventHandler(processing_manager)
    observers = _setup_file_observers(config.SOURCE_DIRECTORIES.value, event_handler)

    directories = config.SOURCE_DIRECTORIES.value

    deduplicator.cleanup_database(directories)
    deduplicator.analyse_all()
    deduplicator.deduplicate_all(
        skip_analyze_phase=True,
    )

    # this is a blocking call which will run indefinitely
    processing_manager.process_queue()

    for observer in observers:
        observer.stop()
        observer.join()


if __name__ == '__main__':
    cli()

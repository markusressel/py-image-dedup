import time

import click

from py_image_dedup.config import DeduplicatorConfig
from py_image_dedup.library.deduplicator import ImageMatchDeduplicator
from py_image_dedup.library.processing_manager import ProcessingManager
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
    deduplicator = ImageMatchDeduplicator(interactive=True)
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
    deduplicator = ImageMatchDeduplicator(interactive=True)
    result = deduplicator.deduplicate_all(
        skip_analyze_phase=skip_analyse_phase,
    )

    echo()
    result.print_to_console()


@cli.command(name="daemon")
@click.option(*get_option_names(PARAM_DRY_RUN), required=False, default=None, is_flag=True,
              help='When set no files or folders will actually be deleted but a preview of '
                   'what WOULD be done will be printed.')
def c_daemon(dry_run: bool):
    echo("Starting daemon...")

    config: DeduplicatorConfig = DeduplicatorConfig()
    if dry_run is not None:
        config.DRY_RUN.value = dry_run

    if config.STATS_ENABLED.value:
        from prometheus_client import start_http_server
        echo("Starting prometheus reporter...")
        start_http_server(config.STATS_PORT.value)

    deduplicator = ImageMatchDeduplicator(interactive=False)
    processing_manager = ProcessingManager(deduplicator)

    deduplicator.deduplicate_all()
    processing_manager.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        processing_manager.stop()


if __name__ == '__main__':
    cli()

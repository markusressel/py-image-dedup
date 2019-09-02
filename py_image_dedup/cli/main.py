import click

from py_image_dedup.config.deduplicator_config import DeduplicatorConfig
from py_image_dedup.library.image_match_deduplicator import ImageMatchDeduplicator
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
    config = DeduplicatorConfig()
    deduplicator = ImageMatchDeduplicator(config)
    deduplicator.analyse()


@cli.command(name="deduplicate")
@click.option(*get_option_names(PARAM_SKIP_ANALYSE_PHASE), required=False, default=False, is_flag=True,
              help='When set the image analysis phase will be skipped. Useful if you already did a dry-run.')
@click.option(*get_option_names(PARAM_DRY_RUN), required=False, default=False, is_flag=True,
              help='When set no files or folders will actually be deleted but a preview of '
                   'what WOULD be done will be printed.')
def c_deduplicate(skip_analyse_phase: bool,
                  dry_run: bool):
    config = DeduplicatorConfig()
    deduplicator = ImageMatchDeduplicator(config)
    result = deduplicator.deduplicate(
        skip_analyze_phase=skip_analyse_phase,
        dry_run=dry_run
    )

    echo()
    result.print_to_console()


if __name__ == '__main__':
    cli()

import click

from py_image_dedup.library.deduplicator_config import DeduplicatorConfig
from py_image_dedup.library.image_match_deduplicator import ImageMatchDeduplicator
from py_image_dedup.persistence.elasticsearchstorebackend import ElasticSearchStoreBackend
from py_image_dedup.util import echo

IMAGE_HASH_MAP = {}


def validate_directory(ctx, param, directories):
    """
    Validates the directory parameter
    """

    # if not directories or len(directories) is 0:
    #     raise click.BadParameter("Missing target directory!")
    #
    # for directory in directories:
    #     if not directories or not os.path.isdir(directory):
    #         raise click.BadParameter("Target directory is not a directory!")


PARAM_DIRECTORIES = "directories"
PARAM_RECURSIVE = "recursive"
PARAM_SEARCH_ACROSS_DIRS = "search-across-dirs"
PARAM_FILE_EXTENSIONS = "file-extensions"
PARAM_ES_HOSTNAME = "es-hostname"
PARAM_THREADS = "threads"
PARAM_MAX_DIST = "maximum-distance"
PARAM_SKIP_ANALYSE_PHASE = "skip-analyse-phase"
PARAM_REMOVE_EMPTY_FOLDERS = "remove-empty-folders"
PARAM_DRY_RUN = "dry-run"

CMD_OPTION_NAMES = {
    PARAM_DIRECTORIES: ['-d', '--directory', '-d', "directories"],
    PARAM_RECURSIVE: ['--recursive', '-r'],
    PARAM_SEARCH_ACROSS_DIRS: ['--search-across-dirs', '-sad'],
    PARAM_FILE_EXTENSIONS: ['--file-extensions', '-fe'],
    PARAM_ES_HOSTNAME: ['--elasticsearch-hostname', '-esh'],
    PARAM_THREADS: ['--threads', '-t'],
    PARAM_MAX_DIST: ['--maximum-distance', '-md'],
    PARAM_SKIP_ANALYSE_PHASE: ['--skip-analyse-phase', '-sap'],
    PARAM_REMOVE_EMPTY_FOLDERS: ['--remove-empty-folders', '-ref', 'remove_empty_folders'],
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


OPTION_DIRECTORIES = click.option(
    *get_option_names(PARAM_DIRECTORIES),
    required=True,
    type=click.Path(exists=True, file_okay=False, dir_okay=True, writable=False,
                    readable=True, resolve_path=True, allow_dash=False),
    multiple=True,
    help='List of directories to deduplicate.')
OPTION_RECURSIVE = click.option(
    *get_option_names(PARAM_RECURSIVE),
    required=False,
    default=False,
    is_flag=True,
    help='When set all directories will be recursively analyzed.')

OPTION_SEARCH_ACROSS_DIRECTORIES = click.option(
    *get_option_names(PARAM_SEARCH_ACROSS_DIRS),
    required=False,
    default=False,
    is_flag=True,
    help='When set duplicates will be found even if they are located in different root directories.')

OPTION_FILE_EXTENSIONS = click.option(
    *get_option_names(PARAM_FILE_EXTENSIONS),
    required=False,
    default="png,jpg,jpeg",
    type=str,
    help='Comma separated list of file extentions.')

OPTION_THREADS = click.option(
    *get_option_names(PARAM_THREADS),
    required=False,
    default=2,
    type=int,
    help='Number of threads to use for image analysis phase.')

OPTION_ES_HOSTNAME = click.option(
    *get_option_names(PARAM_ES_HOSTNAME),
    required=False,
    default="127.0.0.1",
    type=str,
    help='Hostname of the elasticsearch backend instance to use.')


@cli.command(name="analyse")
@OPTION_DIRECTORIES
@OPTION_RECURSIVE
@OPTION_SEARCH_ACROSS_DIRECTORIES
@OPTION_FILE_EXTENSIONS
@OPTION_THREADS
@OPTION_ES_HOSTNAME
def c_analyse(directories: click.Path, recursive: bool, search_across_dirs: bool, file_extensions: str,
              threads: int, elasticsearch_hostname: str):
    deduplicator = ImageMatchDeduplicator(
        image_signature_store=ElasticSearchStoreBackend(
            host=elasticsearch_hostname,
            use_exif_data=True
        )
    )

    config = DeduplicatorConfig(
        recursive=recursive,
        search_across_root_directories=search_across_dirs,
        file_extension_filter=list(map(lambda x: "." + x, file_extensions.split(","))),
    )

    deduplicator.analyse(directories, config, threads)


@cli.command(name="deduplicate")
@OPTION_DIRECTORIES
@OPTION_RECURSIVE
@OPTION_SEARCH_ACROSS_DIRECTORIES
@OPTION_FILE_EXTENSIONS
@OPTION_THREADS
@OPTION_ES_HOSTNAME
@click.option(*get_option_names(PARAM_MAX_DIST), required=False, default=0.10, type=float,
              help='Maximum signature distance [0..1] to query from elasticsearch backend.')
@click.option(*get_option_names(PARAM_SKIP_ANALYSE_PHASE), required=False, default=False, is_flag=True,
              help='When set the image analysis phase will be skipped. Useful if you already did a dry-run.')
@click.option(*get_option_names(PARAM_REMOVE_EMPTY_FOLDERS), required=False, type=bool, default=True,
              help='Whether to remove empty folders or not.')
@click.option(*get_option_names(PARAM_DRY_RUN), required=False, default=False, is_flag=True,
              help='When set no files or folders will actually be deleted but a preview of '
                   'what WOULD be done will be printed.')
def c_deduplicate(directories: click.Path, recursive: bool,
                  search_across_dirs: bool, file_extensions: str,
                  threads: int, elasticsearch_hostname: str,
                  maximum_distance: float, skip_analyse_phase: bool,
                  remove_empty_folders: bool, dry_run: bool):
    deduplicator = ImageMatchDeduplicator(
        image_signature_store=ElasticSearchStoreBackend(
            host=elasticsearch_hostname,
            max_dist=maximum_distance,
            use_exif_data=True
        )
    )

    config = DeduplicatorConfig(
        recursive=recursive,
        search_across_root_directories=search_across_dirs,
        file_extension_filter=list(map(lambda x: "." + x, file_extensions.split(","))),
        # max_file_modification_time_diff=1 * 1000 * 60 * 5,
    )

    result = deduplicator.deduplicate(
        skip_analyze_phase=skip_analyse_phase,
        remove_empty_folders=remove_empty_folders,
        directories=directories,
        config=config,
        threads=threads,
        dry_run=dry_run
    )

    echo()
    result.print_to_console()


if __name__ == '__main__':
    cli()

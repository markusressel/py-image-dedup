import signal

import click

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


@click.command("deduplicate")
@click.option(
    '-d', '--directory',
    callback=validate_directory,
    type=click.Path(exists=True),
    multiple=True,
    help='Top level directory to analyze for duplicate files'
)
@click.option(
    '-r', '--recursive', default=True,
    help='Recursive search in all sub-folders'
)
@click.option(
    '-v', '--verbose', count=True,
    help='Show output while processing'
)
def deduplicate(recursive, directory, verbose):
    from py_image_dedup.library.Deduplicator import Deduplicator
    deduplicator = Deduplicator([directory])

    result = deduplicator.deduplicate(recursive)

    for r in result:
        print(r)


@click.command("analyze")
@click.option(
    '-d', '--directory',
    callback=validate_directory,
    multiple=True,
    help='Top level directory to analyze for duplicate files'
)
@click.option(
    '-r', '--recursive', default=True,
    help='Recursive search in all sub-folders'
)
@click.option(
    '-v', '--verbose', count=True,
    help='Show output while processing')
def analyze(directory, recursive, verbose):
    from py_image_dedup.library.Deduplicator import Deduplicator
    deduplicator = Deduplicator([directory])

    result = deduplicator.analyze(recursive)

    for r in result:
        print(r)


def output_result():
    # sort according to amount of duplicates
    list_of_hasmaps = {}
    for key, value in IMAGE_HASH_MAP.iteritems():
        if len(value) not in list_of_hasmaps:
            list_of_hasmaps[len(value)] = {}

        items_with_n_duplicates = list_of_hasmaps[len(value)]
        items_with_n_duplicates[key] = value

    # output result
    duplicates_count = 0
    for key, value in list_of_hasmaps.iteritems():
        if key <= 1:
            continue

        console_print_default('File Count %s:' % key)

        for key, value in value.iteritems():
            duplicates_count += 1
            console_print_default('%s:' % key)
            for file in value:
                console_print_default('  %s' % file)

    console_print_warn('Duplicates: %s' % duplicates_count)


def console_print_default(message):
    click.echo(str(message))


def console_print_warn(message):
    click.echo(click.style('Warn: %s' % message, fg='yellow'))


def console_print_error(message):
    click.echo(click.style('Error: %s' % message, fg='red'))


if __name__ == '__main__':
    deduplicate()
    analyze()


    def handler(signum, frame):
        output_result()


    signal.signal(signal.SIGINT, handler)

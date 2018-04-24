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
def deduplicate(directory, recursive, verbose):
    from py_image_dedup.library.Deduplicator import Deduplicator
    deduplicator = Deduplicator([directory])

    result = deduplicator.deduplicate(recursive)

    for r in result:
        print(r)


@click.command("analyze")
@click.option(
    '-d', '--directory',
    callback=validate_directory,
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


def console_print_default(message):
    click.echo(str(message))


def console_print_warn(message):
    click.echo(click.style('Warn: %s' % message, fg='yellow'))


def console_print_error(message):
    click.echo(click.style('Error: %s' % message, fg='red'))


if __name__ == '__main__':
    deduplicate()
    analyze()

import os
import signal

import click

IMAGE_HASH_MAP = {}


@click.command()
@click.option(
    '-recursive',
    default=True,
    help='Recursive search'
)
@click.option(
    '-directory',
    help='Directory'
)
@click.option(
    '-remove',
    default=False,
    help='Remove duplicate files automatically'
)
def deduplicate(recursive, directory, remove):
    """Simple program that greets NAME for a total of COUNT times."""

    try:
        check_arguments(recursive, directory)
    except ValueError as e:
        console_print_error(e)
        exit(-1)

    walk_directory(directory, recursive)

    output_result()

    if remove:
        remove_duplicates()


def check_arguments(recursive, directory):
    """
    Simple cmd argument checks
    """

    if not directory or not os.path.isdir(directory):
        raise ValueError("Target directory is not a directory!")


def walk_directory(root_directory, recursive):
    root_directory = root_directory.decode('utf-8')

    if "~" in root_directory:
        console_print_error("Illegal Char in Directory")

    for (root, dirs, files) in os.walk(str(root_directory)):
        # root is the place you're listing
        # dirs is a list of directories directly under root
        # files is a list of files directly under root

        console_print_default('Analyzing "%s" ...' % root)

        for file in files:
            # click.echo('File: %s' % file)
            image_hash = calculate_image_hash(os.path.join(root, file))
            if image_hash not in IMAGE_HASH_MAP:
                IMAGE_HASH_MAP[image_hash] = []

            IMAGE_HASH_MAP[image_hash].append(os.path.join(root, file))

        # if recursive:
        #    for dir in direct_subfolders:
        #    walk_directory(direct_subfolders)


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


def remove_duplicates():
    for key, value in IMAGE_HASH_MAP.iteritems():
        if (len(value)) <= 1:
            continue

        remnant_found = False
        for file in value:
            if "Camera" in file:
                console_print_default("Ignoring '%s' " % file)
                remnant_found = True
                continue
            else:
                if remnant_found:
                    os.remove(file)
                    console_print_warn("Deleted '%s' " % file)
                    continue
                else:
                    remnant_found = True
                    continue


def console_print_default(message):
    click.echo(str(message))


def console_print_warn(message):
    click.echo(click.style('Warn: %s' % message, fg='yellow'))


def console_print_error(message):
    click.echo(click.style('Error: %s' % message, fg='red'))


def handler(signum, frame):
    output_result()


if __name__ == '__main__':
    deduplicate()
    signal.signal(signal.SIGINT, handler)

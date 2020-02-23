import functools
import logging
import traceback

import click

LOGGER = logging.getLogger(__name__)
LOGGER.setLevel(logging.DEBUG)


def echo(text: str = "", color=None):
    """
    Prints a text to the console
    :param text: the text
    :param color: an optional color
    """
    if text is not click.termui and text is not str:
        text = str(text)
    if color:
        text = click.style(text, fg=color)
    LOGGER.debug(text)
    click.echo(text)


def reraise_with_stack(func):
    """
    Decorator used to reraise exceptions occurring within a future.

    :param func: function to decorate
    :return: decorated function
    """

    @functools.wraps(func)
    def wrapped(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            traceback_str = traceback.format_exc()
            raise ValueError("Error occurred. Original traceback is\n%s\n" % traceback_str)

    return wrapped

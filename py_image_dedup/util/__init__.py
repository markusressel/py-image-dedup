import click


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
    click.echo(text)

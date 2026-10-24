"""Control console verbosity independently of persistent file logs."""
import logging


def configure_console_logging(*, quiet: bool, debug: bool = False):
    root = logging.getLogger()
    if debug:
        root.setLevel(logging.DEBUG)
    for handler in root.handlers:
        if handler.name == "fantasma.console":
            handler.setLevel(logging.CRITICAL + 1 if quiet else
                             logging.DEBUG if debug else logging.INFO)

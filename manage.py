#!/usr/bin/env python
"""Django's command-line utility for administrative tasks."""
import os
import sys
from pathlib import Path


def main() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv(Path(__file__).resolve().parent / ".env")
    except ImportError:
        pass  # python-dotenv installed by `make setup`; first-run resilience

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        raise ImportError(
            "Couldn't import Django. Did you forget to run `make setup` "
            "(or activate a virtualenv)?"
        ) from exc
    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()

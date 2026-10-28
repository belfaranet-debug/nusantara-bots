"""Allow ``python3 -m loader`` (defaults to the ``check`` command)."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main(["check"]))

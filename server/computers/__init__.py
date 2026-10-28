"""Computer registry — local "Sand Machine" record (Phase 1).

Importable as ``computers`` from ``~/workspace/grokbot-own/``.
See ``registry.py`` for the API and ``README.md`` for the design.
"""

from .devices_merge import merge_machine_devices, primary_machine
from .registry import (
    MAX_CAPABILITIES,
    MAX_CAPABILITY_CHARS,
    MAX_LABEL_CHARS,
    MAX_NOTES_CHARS,
    OWNER,
    STATUSES,
    ComputerRegistry,
    DEFAULT_COMPUTERS_FILE,
)

__all__ = [
    "ComputerRegistry",
    "DEFAULT_COMPUTERS_FILE",
    "MAX_CAPABILITIES",
    "MAX_CAPABILITY_CHARS",
    "MAX_LABEL_CHARS",
    "MAX_NOTES_CHARS",
    "OWNER",
    "STATUSES",
    "merge_machine_devices",
    "primary_machine",
]

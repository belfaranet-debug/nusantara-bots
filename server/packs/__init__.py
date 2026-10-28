"""Dots-style plugin-pack format: manifest validator + shelf converter.

Note: the converter lives in packs.convert and is imported directly
(`from packs.convert import ...`); it is deliberately NOT imported here,
so `python3 -m packs.convert` does not double-import the module.
"""

from .pack import (
    DEFAULT_REVIEW_ACTIONS,
    PackResult,
    find_secret_keys,
    validate_manifest,
    validate_pack,
    validate_shelf,
)

__all__ = [
    "DEFAULT_REVIEW_ACTIONS",
    "PackResult",
    "find_secret_keys",
    "validate_manifest",
    "validate_pack",
    "validate_shelf",
]

"""Model alias selection helpers shared by protocol handlers."""

from __future__ import annotations

from fnmatch import fnmatchcase
from typing import Any


def select_alias_model(model: str, aliases: Any) -> str:
    """Return exact alias target first, then first matching wildcard target."""
    if not isinstance(aliases, dict):
        return ""

    exact_match = aliases.get(model, "")
    if exact_match:
        return exact_match

    for alias_pattern, target_model in aliases.items():
        if "*" in alias_pattern and fnmatchcase(model, alias_pattern):
            return target_model

    return ""

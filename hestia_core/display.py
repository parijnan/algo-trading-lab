"""How an engine's registry name (lower case, e.g. 'prometheus') is written wherever a person reads it: Slack and the session report.
Logs and file names keep the registry name, so greps and paths stay as they are."""

from __future__ import annotations


def display_name(name: str) -> str:
    return name[:1].upper() + name[1:] if name else name

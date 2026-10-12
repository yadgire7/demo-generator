"""Plugin lookup through entry points: `demogen.llm`, and later `demogen.tts`, `.stt`, `.driver`, `.recorder`."""

from __future__ import annotations

from importlib.metadata import entry_points
from typing import Any

from demogen.errors import DemogenError


class UnknownPlugin(DemogenError):
    pass


_LOCAL: dict[tuple[str, str], Any] = {}


def register_plugin(group: str, name: str, plugin: Any) -> None:
    """Register a plugin in this process, ahead of any installed one of the same name."""
    _LOCAL[(group, name)] = plugin


def load_plugin(group: str, name: str) -> Any:
    if (group, name) in _LOCAL:
        return _LOCAL[(group, name)]
    matches = entry_points(group=f"demogen.{group}", name=name)
    if not matches:
        known = sorted(ep.name for ep in entry_points(group=f"demogen.{group}"))
        raise UnknownPlugin(f"no {group} plugin named {name!r} (installed: {', '.join(known) or 'none'})")
    return next(iter(matches)).load()

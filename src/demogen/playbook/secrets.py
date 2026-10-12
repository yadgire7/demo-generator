"""`${secret:NAME}` references: resolved only at the point of use, redacted everywhere else."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping

from demogen.errors import DemogenError

_SECRET = re.compile(r"\$\{secret:([A-Za-z_][A-Za-z0-9_]*)\}")
REDACTED = "[REDACTED]"


class MissingSecret(DemogenError):
    pass


def secret_names(text: str) -> set[str]:
    return set(_SECRET.findall(text))


def resolve_secrets(text: str, env: Mapping[str, str] | None = None) -> str:
    env = os.environ if env is None else env

    def lookup(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in env:
            raise MissingSecret(f"secret {name} is not set in the environment")
        return env[name]

    return _SECRET.sub(lookup, text)


class Redactor:
    """Strips known secret values from text bound for logs or an LLM prompt."""

    def __init__(self, values: list[str]):
        # Longest first, so a secret that contains another is removed whole.
        self._values = sorted({v for v in values if v}, key=len, reverse=True)

    @classmethod
    def for_text(cls, text: str, env: Mapping[str, str] | None = None) -> Redactor:
        env = os.environ if env is None else env
        return cls([env[name] for name in secret_names(text) if name in env])

    def redact(self, text: str) -> str:
        for value in self._values:
            text = text.replace(value, REDACTED)
        return text

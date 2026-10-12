"""The playbook: what the demo shows, written as intent-level steps."""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator

from demogen.playbook.actions import Action

_DURATION = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(ms|s|m)?\s*$")
_UNIT_SECONDS = {"ms": 0.001, "s": 1.0, "m": 60.0, None: 1.0}


def parse_duration(value: Any) -> float:
    """Accept seconds as a number, or a string such as "1.5s", "500ms" or "2m"."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    match = _DURATION.match(value) if isinstance(value, str) else None
    if not match:
        raise ValueError(f"not a duration: {value!r} (use e.g. 1.5s, 500ms, 2m)")
    return float(match.group(1)) * _UNIT_SECONDS[match.group(2)]


Duration = Annotated[float, BeforeValidator(parse_duration), Field(ge=0)]


class _Model(BaseModel):
    # A typo in a playbook key should fail loudly, not be silently ignored.
    model_config = ConfigDict(extra="forbid")


class Viewport(_Model):
    width: int = 1440
    height: int = 900
    device_scale_factor: float = 2


class Auth(_Model):
    profile: str


class Target(_Model):
    kind: Literal["web"] = "web"
    base_url: str
    auth: Auth | None = None
    viewport: Viewport = Viewport()


class SetupStep(_Model):
    do: str


class Setup(_Model):
    seed: str | None = None
    steps: list[SetupStep] = []


class Intent(_Model):
    audience: str | None = None
    tone: str | None = None
    key_messages: list[str] = []
    cta: str | None = None
    notes: str | None = None


class Step(_Model):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")
    do: str
    expect: str | None = None
    say_hint: str | None = None
    dwell: Duration = 0
    data: dict[str, Any] = {}
    focus: str | None = None
    action: list[Action] | None = None  # pins the step's actions; skips LLM resolution


class Playbook(_Model):
    version: Literal[1]
    name: str
    target: Target
    setup: Setup = Setup()
    intent: Intent = Intent()
    steps: list[Step] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_step_ids(self) -> Playbook:
        seen: set[str] = set()
        for step in self.steps:
            if step.id in seen:
                raise ValueError(f"duplicate step id: {step.id}")
            seen.add(step.id)
        return self

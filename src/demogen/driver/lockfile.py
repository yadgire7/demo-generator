from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from demogen.playbook.actions import Action


class ResolvedStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    step_id: str
    # "playbook": the author pinned these actions. "llm": resolved in rehearsal. "stub": placeholder.
    source: Literal["playbook", "llm", "stub"]
    actions: list[Action] = []
    # Text that must be visible once the step has run; checked on every replay.
    expect_text: list[str] = []


class Plan(BaseModel):
    """actions.lock.json: the concrete actions a take replays."""

    model_config = ConfigDict(extra="forbid")

    steps: list[ResolvedStep]

    def step(self, step_id: str) -> ResolvedStep:
        return next(step for step in self.steps if step.step_id == step_id)

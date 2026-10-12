from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class StepFacts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    step_id: str
    happens: str  # what a viewer sees happen in this step
    on_screen: list[str]  # text on screen that matters to the step, exactly as shown


class Facts(BaseModel):
    """facts.json: what the recorded footage actually shows, step by step."""

    model_config = ConfigDict(extra="forbid")

    steps: list[StepFacts]


class StoryStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    step_id: str
    text: str


class Story(BaseModel):
    """story.json: the narration, one passage per playbook step."""

    model_config = ConfigDict(extra="forbid")

    steps: list[StoryStep]

    def text_of(self, step_id: str) -> str:
        return next(step.text for step in self.steps if step.step_id == step_id)

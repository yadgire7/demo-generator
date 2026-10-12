"""Turn the take's keyframes into facts: what each step actually shows on screen."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from demogen.llm.base import LLMProvider, image_part
from demogen.llm.structured import Problems, complete_checked
from demogen.playbook.schema import Playbook
from demogen.store.artifacts import ArtifactRef
from demogen.story.schema import Facts, StepFacts

MAX_IMAGES_PER_CALL = 20

SYSTEM = """\
You are shown frames from a screen recording of a product demo, grouped by step. For each \
step you are told what the presenter did, and you see the screen as it was left afterwards.

For every step, report:
- happens: what a viewer sees happen on screen in that step, in one or two plain sentences. \
Describe the visible change, not the clicking.
- on_screen: the text on screen that matters to that step (labels, values, counts, messages), \
copied exactly as it appears.

Report only what the frames show. If they do not show what the step was meant to achieve, say \
so in `happens`. Your notes go to a writer who narrates the video without seeing it, so they \
depend on you for what is really there."""


def describe_frames(
    llm: LLMProvider, playbook: Playbook, manifest: dict[str, Any], image_path: Callable[[ArtifactRef], Path]
) -> Facts:
    """One model call per batch of steps; each batch opens with the screen as its first step found it."""
    steps = {step.id: step for step in playbook.steps}
    facts: list[StepFacts] = []
    for batch in _batches(manifest["steps"]):
        content: list[dict[str, Any]] = []

        def show(frame: dict[str, Any]) -> None:
            content.append({"type": "text", "text": f"Frame at {frame['t']:.1f}s, {frame['caption']}:"})
            content.append(image_part(image_path(frame["image"])))

        content.append({"type": "text", "text": "The screen before the first of these steps:"})
        show(batch[0]["before"])
        for entry in batch:
            step = steps[entry["step_id"]]
            brief = f"Step `{step.id}`. The presenter: {step.do}"
            if step.expect:
                brief += f"\nMeant to achieve: {step.expect}"
            content.append({"type": "text", "text": brief})
            for frame in entry["frames"]:
                show(frame)
        expected = [entry["step_id"] for entry in batch]
        content.append({"type": "text", "text": f"Report on these steps, in this order: {', '.join(expected)}."})

        def check(reply: Facts, expected: list[str] = expected) -> Problems:
            got = [step.step_id for step in reply.steps]
            return ([f"report on exactly these steps, in this order: {expected} (got {got})"] if got != expected else [], [])

        messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": content}]
        facts += complete_checked(llm, messages, Facts, check).steps
    return Facts(steps=facts)


def _batches(entries: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    batches: list[list[dict[str, Any]]] = []
    images = 0
    for entry in entries:
        needed = len(entry["frames"])
        if not batches or images + needed > MAX_IMAGES_PER_CALL:
            batches.append([])
            images = 1  # the opening "before" frame
        batches[-1].append(entry)
        images += needed
    return batches

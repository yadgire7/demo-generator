"""The story stages: see what the take shows, then write narration that fits it."""

from __future__ import annotations

import json
from typing import Any

from demogen import media
from demogen.llm.base import llm_for
from demogen.playbook.schema import Intent, Playbook
from demogen.playbook.secrets import Redactor
from demogen.stages.base import StageContext, StageError, stage
from demogen.story.budget import Budget, speech_seconds
from demogen.story.keyframes import extract_frames, plan_keyframes, scene_changes
from demogen.story.schema import Facts, Story
from demogen.story.vision import describe_frames
from demogen.story.writer import rewrite_passages, write_narration


def _playbook(ctx: StageContext, inputs: dict[str, Any]) -> Playbook:
    return Playbook.model_validate(ctx.store.get_json(inputs["playbook"]))


@stage("extract_keyframes", config_keys=("keyframe_max_edge",))
def extract_keyframes(ctx: StageContext, inputs: dict[str, Any]) -> dict[str, Any]:
    """Save the frames that show each step: the screen before it, and after each of its actions."""
    if not inputs.get("video"):
        raise StageError("there is no recorded video to take frames from")
    video = ctx.store.local_path(inputs["video"])
    events = [json.loads(line) for line in ctx.store.get_bytes(inputs["events"]["path"]).decode().splitlines()]
    windows = ctx.store.get_json(inputs["windows"])
    plan = plan_keyframes(events, windows, scene_changes(video), media.probe(video).fps)

    flat = [frame for _, before, frames in plan for frame in (before, *frames)]
    images = iter(extract_frames(video, [frame.t for frame in flat], ctx.config.keyframe_max_edge))
    steps = []
    for step_id, before, frames in plan:
        saved = [
            {"t": round(frame.t, 3), "caption": frame.caption, "image": ctx.store.put_bytes(f"keyframes/{step_id}/{n:02d}.png", next(images))}
            for n, frame in enumerate((before, *frames))
        ]
        steps.append({"step_id": step_id, "before": saved[0], "frames": saved[1:]})
    return {"keyframes": ctx.store.put_json("keyframes/manifest.json", {"steps": steps})}


@stage("analyze_frames", config_keys=("models.vision",))
def analyze_frames(ctx: StageContext, inputs: dict[str, Any]) -> dict[str, Any]:
    """Have a vision model report what each step's frames show, so the narration is true to the footage."""
    playbook = _playbook(ctx, inputs)
    manifest = ctx.store.get_json(inputs["keyframes"])
    facts = describe_frames(llm_for(ctx.config, "vision"), playbook, manifest, ctx.store.local_path)
    # A secret the app displays (a signed-in user's name, say) must not travel on into the story.
    redactor = Redactor.for_text(playbook.model_dump_json())
    for step in facts.steps:
        step.happens = redactor.redact(step.happens)
        step.on_screen = [redactor.redact(text) for text in step.on_screen]
    return {"facts": ctx.store.put_json("facts.json", facts.model_dump(mode="json"))}


@stage("write_story", config_keys=("target_wps", "models.writer"))
def write_story(ctx: StageContext, inputs: dict[str, Any]) -> dict[str, Any]:
    """Write the narration, or shorten the passages that ran too long once they were voiced."""
    playbook = _playbook(ctx, inputs)
    intent = Intent.model_validate(ctx.store.get_json(inputs["intent"]))
    facts = Facts.model_validate(ctx.store.get_json(inputs["facts"]))
    wps = ctx.config.target_wps
    budgets = {
        step_id: Budget.for_window(window["end"] - window["start"], wps)
        for step_id, window in ctx.store.get_json(inputs["windows"]).items()
    }
    llm = llm_for(ctx.config, "writer")
    flagged = inputs.get("flagged") or []
    if flagged and inputs.get("story"):
        current = Story.model_validate(ctx.store.get_json(inputs["story"]))
        clips = inputs.get("clips") or {}
        spoken = {}
        for step_id in flagged:
            text = current.text_of(step_id)
            spoken[step_id] = clips[step_id]["duration"] if step_id in clips else speech_seconds(text, wps)
            budgets[step_id] = budgets[step_id].tightened(len(text.split()), spoken[step_id])
        story = rewrite_passages(llm, playbook, intent, facts, budgets, current, spoken)
    else:
        story = write_narration(llm, playbook, intent, facts, budgets)
    return {"story": ctx.store.put_json("story.json", story.model_dump(mode="json"))}

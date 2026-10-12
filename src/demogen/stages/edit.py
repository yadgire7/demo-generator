from __future__ import annotations

from typing import Any

from demogen.stages.base import REGISTRY, StageContext, stage
from demogen.story.budget import MAX_OVERRUN


@stage("plan_timeline", stub=True)
def plan_timeline(ctx: StageContext, inputs: dict[str, Any]) -> dict[str, Any]:
    # M4: the full edit decision list (speed, freeze-frames, overlays). Here: the pairing of
    # each step's footage window with its narration length, and the overrun check.
    windows = ctx.store.get_json(inputs["windows"])
    steps, flagged = [], []
    for step_id, window in sorted(windows.items(), key=lambda item: item[1]["start"]):
        duration = inputs["clips"][step_id]["duration"]
        if duration > MAX_OVERRUN * (window["end"] - window["start"]):
            flagged.append(step_id)
        steps.append({"step_id": step_id, "video": window, "audio_duration": duration})
    return {"edl": ctx.store.put_json("edl.json", {"stub": True, "steps": steps}), "flagged": flagged}


@stage("render", stub=True)
def render(ctx: StageContext, inputs: dict[str, Any]) -> dict[str, Any]:
    # M4: composite cursor, ripples, zoom and captions, mix the audio, and encode final.mp4.
    return {"final": {"video": None, "srt": None}}


@stage("final_qa")
def final_qa(ctx: StageContext, inputs: dict[str, Any]) -> dict[str, Any]:
    edl = ctx.store.get_json(inputs["edl"])
    report = {
        "run_id": ctx.run_id,
        "steps": len(edl["steps"]),
        "final": inputs["final"],
        "overrunning_steps": inputs.get("flagged") or [],
        # Clips that speech-to-text heard differently from their script: worth a listen.
        "audio_warnings": ctx.store.get_json(inputs["words"]).get("warnings", []),
        # Stages still running as placeholders; the run is not a real demo while any remain.
        "stub_stages": sorted(name for name, spec in REGISTRY.items() if spec.stub),
    }
    return {"report": ctx.store.put_json("report.json", report)}

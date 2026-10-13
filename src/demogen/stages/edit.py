"""The edit stages: plan the timeline, render the finished video, and check it."""

from __future__ import annotations

import json
import statistics
import tempfile
from pathlib import Path
from typing import Any

from demogen import media
from demogen.editor.compositor import render_video
from demogen.stages.base import StageContext, StageError, stage
from demogen.timeline.captions import to_srt
from demogen.timeline.edl import EDL
from demogen.timeline.planner import plan_timeline as plan
from demogen.voice.check import check_clip
from demogen.voice.providers import stt_for

# How far the finished video may stray from its plan before the report says so.
DURATION_TOLERANCE_S = 0.1
DRIFT_TOLERANCE_S = 0.35


def _events(ctx: StageContext, inputs: dict[str, Any]) -> list[dict[str, Any]]:
    return [json.loads(line) for line in ctx.store.get_bytes(inputs["events"]["path"]).decode().splitlines()]


def _video(ctx: StageContext, inputs: dict[str, Any]) -> Path:
    if not inputs.get("video"):
        raise StageError("there is no recorded video to edit")
    return ctx.store.local_path(inputs["video"])


@stage("plan_timeline", config_keys=("render.zoom",))
def plan_timeline(ctx: StageContext, inputs: dict[str, Any]) -> dict[str, Any]:
    """Decide where each narration clip plays, where a frame is held to make room, and where to zoom."""
    events = _events(ctx, inputs)
    meta = next(e for e in events if e["type"] == "meta")
    info = media.probe(_video(ctx, inputs))
    edl, flagged = plan(
        windows=ctx.store.get_json(inputs["windows"]),
        clips=inputs["clips"],
        words=ctx.store.get_json(inputs["words"])["steps"],
        events=events,
        video_seconds=info.seconds,
        fps=info.fps,
        viewport=tuple(meta["viewport"]),
        zoom=ctx.config.render.zoom,
    )
    return {"edl": ctx.store.put_json("edl.json", edl.model_dump(mode="json")), "flagged": flagged}


@stage("render", config_keys=("render",))
def render(ctx: StageContext, inputs: dict[str, Any]) -> dict[str, Any]:
    """Produce final.mp4 (pointer, click ripples, zoom, captions, narration) and final.srt."""
    edl = EDL.model_validate(ctx.store.get_json(inputs["edl"]))
    settings = ctx.config.render
    clips = {cue.step_id: ctx.store.local_path(cue.clip) for cue in edl.audio if cue.clip}
    with tempfile.TemporaryDirectory() as scratch:
        out = Path(scratch) / "final.mp4"
        render_video(
            _video(ctx, inputs), edl, _events(ctx, inputs), clips, out,
            height=settings.height, crf=settings.crf, pointer=settings.pointer, captions=settings.captions, font_path=settings.font,
        )  # fmt: skip
        video = ctx.store.put_file("final.mp4", out)
    return {"final": {"video": video, "srt": ctx.store.put_bytes("final.srt", to_srt(edl.captions).encode())}}


def _check_final(ctx: StageContext, inputs: dict[str, Any], edl: EDL) -> dict[str, Any]:
    """Measure the finished video against its plan: length, what is said, and when."""
    path = ctx.store.local_path(inputs["final"]["video"])
    info = media.probe(path)
    check: dict[str, Any] = {"seconds": info.seconds, "planned_seconds": edl.duration, "size": [info.width, info.height], "problems": []}
    if abs(info.seconds - edl.duration) > DURATION_TOLERANCE_S:
        check["problems"].append(f"the video runs {info.seconds:.2f}s but was planned at {edl.duration:.2f}s")

    cues = [cue for cue in edl.audio if cue.clip]
    if cues:
        story = {step["step_id"]: step["text"] for step in ctx.store.get_json(inputs["story"])["steps"]}
        words = ctx.store.get_json(inputs["words"])["steps"]
        heard = check_clip(" ".join(story[cue.step_id] for cue in cues), stt_for(ctx.config.voice.stt).transcribe(media.extract_wav(path)), info.seconds)
        check["narration_wer"] = round(heard.wer, 3)
        if heard.wer > ctx.config.voice.max_wer:
            check["problems"].append(f"the narration in the final mix does not match the story (word error rate {heard.wer:.2f})")
        # Drift: how far from its planned time each passage is actually heard, by its typical word.
        drifts, index = {}, 0
        for cue in cues:
            planned = [cue.at + word["end"] / cue.tempo for word in words[cue.step_id]]
            actual = [word["end"] for word in heard.words[index : index + len(planned)]]
            index += len(planned)
            if planned:
                drifts[cue.step_id] = round(statistics.median(a - p for a, p in zip(actual, planned)), 3)
        check["drift_seconds"] = drifts
        late = {step_id: drift for step_id, drift in drifts.items() if abs(drift) > DRIFT_TOLERANCE_S}
        if late:
            check["problems"].append(f"narration is off its planned time by more than {DRIFT_TOLERANCE_S}s: {late}")
    return check


@stage("final_qa", config_keys=("voice.stt", "voice.max_wer"))
def final_qa(ctx: StageContext, inputs: dict[str, Any]) -> dict[str, Any]:
    edl = ctx.store.get_json(inputs["edl"])
    report = {
        "run_id": ctx.run_id,
        "steps": len(edl["steps"]),
        "final": inputs["final"],
        "overrunning_steps": inputs.get("flagged") or [],
        # Clips that speech-to-text heard differently from their script: worth a listen.
        "audio_warnings": ctx.store.get_json(inputs["words"]).get("warnings", []),
    }
    if inputs["final"].get("video"):
        report["final_check"] = _check_final(ctx, inputs, EDL.model_validate(edl))
    return {"report": ctx.store.put_json("report.json", report)}

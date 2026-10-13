"""Fit the narration to the take: where each clip plays, where a frame is held, where to zoom.

Pure and deterministic: the same take, clips and settings always give the same plan.
"""

from __future__ import annotations

from typing import Any

from demogen.story.budget import MAX_OVERRUN
from demogen.timeline.captions import build_cues
from demogen.timeline.edl import EDL, AudioCue, Caption, Segment, StepSpan, ZoomKey

LEAD_S = 0.1  # narration starts just after its step does
GAP_S = 0.2  # and leaves a breath before the next step
MAX_TEMPO = 1.1  # a clip may be sped up this much to fit; beyond that a frame is held instead

ZOOM_EASE_S = 0.5
ZOOM_LEAD_S = 0.6  # the zoom starts this long before the step's first action
MIN_ZOOMED_S = 0.6  # shorter than this and the zoom is only a twitch
ZOOM_FILL = 0.9  # the acted-on elements must fit within this share of the zoomed view
_ELEMENT_ACTIONS = ("click", "fill", "hover", "select")


def fit(clip_s: float, window_s: float) -> tuple[float, float]:
    """How to fit a clip to its step's footage: (tempo, seconds to hold the step's last frame)."""
    room = window_s - LEAD_S - GAP_S
    if clip_s <= room:
        return 1.0, 0.0
    if room > 0 and clip_s <= MAX_TEMPO * room:
        return round(clip_s / room, 4), 0.0
    return 1.0, round(clip_s - room, 3)


def plan_timeline(
    *,
    windows: dict[str, dict[str, float]],
    clips: dict[str, dict[str, Any]],
    words: dict[str, list[dict[str, Any]]],
    events: list[dict[str, Any]],
    video_seconds: float,
    fps: float,
    viewport: tuple[int, int],
    zoom: float,
) -> tuple[EDL, list[str]]:
    """The edit decision list, and the steps whose narration is too long even for a held frame."""
    order = sorted(windows, key=lambda step_id: windows[step_id]["start"])
    segments: list[Segment] = []
    audio: list[AudioCue] = []
    steps: list[StepSpan] = []
    captions: list[Caption] = []
    flagged: list[str] = []
    source_from = output_t = 0.0
    for step_id in order:
        start, end = windows[step_id]["start"], windows[step_id]["end"]
        clip = clips[step_id]
        tempo, hold = fit(clip["duration"], end - start)
        if clip["duration"] > MAX_OVERRUN * (end - start):
            flagged.append(step_id)
        step_at = output_t + (start - source_from)
        cue = AudioCue(step_id=step_id, clip=clip.get("audio"), at=round(step_at + LEAD_S, 3), tempo=tempo, duration=round(clip["duration"] / tempo, 3))
        audio.append(cue)
        captions += build_cues([(w["w"], cue.at + w["start"] / tempo, cue.at + w["end"] / tempo) for w in words.get(step_id, [])])
        segments.append(Segment(source_start=source_from, source_end=end, at=round(output_t, 3), hold=hold))
        output_t = segments[-1].end
        steps.append(StepSpan(step_id=step_id, start=round(step_at, 3), end=round(output_t, 3)))
        source_from = end
    segments.append(Segment(source_start=source_from, source_end=video_seconds, at=round(output_t, 3)))

    edl = EDL(fps=fps, viewport=viewport, duration=round(segments[-1].end, 3), segments=segments, audio=audio, zoom=[], captions=captions, steps=steps)
    if zoom > 1:
        edl.zoom = _zoom_keys(edl, order, windows, events, zoom)
    return edl, flagged


def _zoom_keys(edl: EDL, order: list[str], windows: dict[str, dict[str, float]], events: list[dict[str, Any]], zoom: float) -> list[ZoomKey]:
    """Zoom in on what each step acts on, and back out once the screen has settled to show the result."""
    width, height = edl.viewport
    full = (0.0, 0.0, float(width), float(height))
    view_w, view_h = width / zoom, height / zoom
    keys = [ZoomKey(t=0.0, rect=full)]
    for step_id in order:
        acted = [e for e in events if e["type"] == "action" and e.get("step_id") == step_id and e["action"] in _ELEMENT_ACTIONS and e.get("bbox")]
        if not acted:
            continue
        left = min(e["bbox"]["x"] for e in acted)
        top = min(e["bbox"]["y"] for e in acted)
        right = max(e["bbox"]["x"] + e["bbox"]["width"] for e in acted)
        bottom = max(e["bbox"]["y"] + e["bbox"]["height"] for e in acted)
        if right - left > ZOOM_FILL * view_w or bottom - top > ZOOM_FILL * view_h:
            continue  # too spread out to frame; stay wide

        settled = next((e["t"] for e in events if e["type"] == "settled" and e.get("step_id") == step_id), windows[step_id]["end"])
        zoom_in = max(windows[step_id]["start"], acted[0]["t"] - ZOOM_LEAD_S)
        if settled - (zoom_in + ZOOM_EASE_S) < MIN_ZOOMED_S:
            continue
        x = min(max((left + right) / 2 - view_w / 2, 0.0), width - view_w)
        y = min(max((top + bottom) / 2 - view_h / 2, 0.0), height - view_h)
        close = (round(x, 1), round(y, 1), round(view_w, 1), round(view_h, 1))
        # Never before this step starts in the output (a held frame may come first), nor before
        # the last zoom has finished.
        step_at = next(span.start for span in edl.steps if span.step_id == step_id)
        in_at = max(edl.to_output(zoom_in), step_at, keys[-1].t)
        out_at = edl.to_output(settled)
        keys += [
            ZoomKey(t=round(in_at, 3), rect=full),
            ZoomKey(t=round(in_at + ZOOM_EASE_S, 3), rect=close),
            ZoomKey(t=round(out_at, 3), rect=close),
            ZoomKey(t=round(out_at + ZOOM_EASE_S, 3), rect=full),
        ]
    return keys if len(keys) > 1 else []

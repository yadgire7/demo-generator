"""Choose the frames that show what each step did, and pull them out of the take."""

from __future__ import annotations

import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from demogen import media

# How strongly two consecutive frames must differ to count as the screen changing: roughly,
# the share of the picture that changed completely. Measured on real takes: a typed character
# scores about 0.0005, a button's hover highlight up to 0.0025, a ticked checkbox about 0.003,
# a new list row 0.01, a toast notification 0.015 to 0.04. The threshold sits above the hover
# highlights, which come and go as the pointer travels and are not worth a frame of their own.
SCENE_THRESHOLD = 0.004
MIN_HELD_SECONDS = 0.2  # shorter-lived states are animation frames, not something to describe
MAX_PASSING_FRAMES = 2  # per step

_SCENE_TIME = re.compile(r"pts_time:([\d.]+)")


@dataclass(frozen=True)
class Frame:
    t: float
    caption: str


def scene_changes(video: Path) -> list[float]:
    """Times at which the picture visibly changes."""
    out = media.run(
        "ffmpeg", "-loglevel", "error", "-i", str(video),
        "-vf", f"scale=480:-2,select='gt(scene,{SCENE_THRESHOLD})',metadata=print:file=-", "-an", "-f", "null", "-",
    )  # fmt: skip
    return [float(t) for t in _SCENE_TIME.findall(out.stdout)]


def _describe(event: dict[str, Any]) -> str:
    return " ".join(str(part) for part in (event["action"], event.get("label") or event.get("detail")) if part)


def plan_keyframes(
    events: list[dict[str, Any]], windows: dict[str, dict[str, float]], changes: list[float], fps: float
) -> list[tuple[str, Frame, list[Frame]]]:
    """Per step, in take order: its id, the frame before it starts, and the frames that show it.

    A step is shown by the settled screen after each of its actions, plus any state that came
    and went in between (a toast, a progress message) and so appears in none of those.
    """
    tick = 1 / fps
    plan = []
    for step_id, window in sorted(windows.items(), key=lambda item: item[1]["start"]):
        actions = [e for e in events if e["type"] == "action" and e.get("step_id") == step_id]
        # The screen has settled after an action at the last moment before the next thing happens.
        settled = [later["t"] - tick for later in actions[1:]] + [window["end"] - tick]
        if actions:
            frames = [Frame(t, f"after: {_describe(action)}") for action, t in zip(actions, settled)]
        else:
            frames = [Frame(settled[-1], "end of the step")]

        shown = [window["start"], *(frame.t for frame in frames)]
        passing = []
        for earlier, later in zip(shown, shown[1:]):
            between = [t for t in changes if earlier < t < later]
            # The state after the last change is what the next keyframe shows; any state before
            # that one was replaced before a keyframe could show it.
            passing += [(b - a, (a + b) / 2) for a, b in zip(between, between[1:]) if b - a >= MIN_HELD_SECONDS]
        for _, t in sorted(passing, reverse=True)[:MAX_PASSING_FRAMES]:
            frames.append(Frame(t, "briefly on screen during the step"))

        plan.append((step_id, Frame(window["start"], "before the step"), sorted(frames, key=lambda f: f.t)))
    return plan


def extract_frames(video: Path, times: list[float], max_edge: int) -> list[bytes]:
    """PNG images of the video at the given times, scaled down so neither side exceeds `max_edge`."""
    info = media.probe(video)
    numbers = [min(max(round(t * info.fps), 0), info.frames - 1) for t in times]
    wanted = sorted(set(numbers))
    select = "+".join(rf"eq(n\,{n})" for n in wanted)
    scale = f"scale=w='if(gt(iw,ih),min(iw,{max_edge}),-2)':h='if(gt(iw,ih),-2,min(ih,{max_edge}))'"
    with tempfile.TemporaryDirectory() as scratch:
        media.run(
            "ffmpeg", "-loglevel", "error", "-i", str(video),
            "-vf", f"select='{select}',{scale}", "-fps_mode", "passthrough", f"{scratch}/%05d.png",
        )  # fmt: skip
        images = [path.read_bytes() for path in sorted(Path(scratch).glob("*.png"))]
    if len(images) != len(wanted):
        raise media.MediaError(f"asked the video for {len(wanted)} frames and got {len(images)}")
    by_number = dict(zip(wanted, images))
    return [by_number[n] for n in numbers]

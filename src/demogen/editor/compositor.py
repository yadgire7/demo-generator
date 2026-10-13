"""Render the finished video: the take, re-timed and framed by the edit list, with overlays and sound.

ffmpeg decodes the take to raw frames; each output frame is cropped and scaled to the zoom in
force, drawn over, and piped to a second ffmpeg that encodes it with the narration mixed in.
"""

from __future__ import annotations

import bisect
import subprocess
from pathlib import Path
from typing import Any

from PIL import Image

from demogen import media
from demogen.editor import overlays
from demogen.timeline.edl import EDL

LOUDNESS = "loudnorm=I=-16:TP=-1.5:LRA=11"  # the level streaming platforms expect for speech


def output_size(source: media.VideoInfo, height: int | None) -> tuple[int, int]:
    """The finished video's size: the take's own, or scaled to `height`. Both sides even."""
    if height is None or height >= source.height:
        return source.width - source.width % 2, source.height - source.height % 2
    height -= height % 2
    width = round(source.width * height / source.height)
    return width - width % 2, height


def render_video(
    source: Path,
    edl: EDL,
    events: list[dict[str, Any]],
    clips: dict[str, Path],
    out: Path,
    *,
    height: int | None,
    crf: int,
    pointer: bool = True,
    captions: bool = True,
    font_path: str | None = None,
) -> None:
    info = media.probe(source)
    width, height = output_size(info, height)
    per_css = info.width / edl.viewport[0]  # source pixels per CSS pixel
    frame_count = round(edl.duration * edl.fps)

    # Pointer positions and clicks, moved onto the output clock.
    track = [(edl.to_output(e["t"]), e["x"], e["y"]) for e in events if e["type"] == "cursor"]
    track_times = [t for t, _, _ in track]
    clicks = [(edl.to_output(e["t"]), e["x"], e["y"]) for e in events if e["type"] == "click"]

    def pointer_at(t: float) -> tuple[float, float] | None:
        if not track:
            return None
        index = bisect.bisect_right(track_times, t)
        if index == 0:
            return track[0][1:]
        if index == len(track):
            return track[-1][1:]
        (t0, x0, y0), (t1, x1, y1) = track[index - 1], track[index]
        p = (t - t0) / (t1 - t0) if t1 > t0 else 1.0
        return x0 + (x1 - x0) * p, y0 + (y1 - y0) * p

    unit = height / 900 * 1.3
    sprite, tip = overlays.pointer(round(unit, 2))
    halo = overlays.halo(round(height * 0.024))
    ripple_radius = round(height * 0.045)
    font_px = round(height * 0.034)

    decoder = subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(source), "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"],
        stdout=subprocess.PIPE,
    )
    encoder = subprocess.Popen(_encode_command(edl, clips, out, (width, height), crf), stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    frame_bytes = info.width * info.height * 3
    raw, loaded = b"", -1
    previous_key, previous_frame = None, b""
    try:
        for number in range(frame_count):
            t = number / edl.fps
            wanted = min(round(edl.source_at(t) * info.fps), info.frames - 1)
            while loaded < wanted:
                chunk = decoder.stdout.read(frame_bytes)
                if len(chunk) < frame_bytes:
                    break  # the take ran out a frame early; keep showing the last one
                raw, loaded = chunk, loaded + 1

            rect = edl.rect_at(t)
            at = pointer_at(t) if pointer else None
            rings = tuple((round((t - ct) / overlays.RIPPLE_S, 2), cx, cy) for ct, cx, cy in clicks if 0 <= t - ct < overlays.RIPPLE_S)
            text = next((c.text for c in edl.captions if c.start <= t < c.end), None) if captions else None
            key = (loaded, tuple(round(v, 2) for v in rect), at and (round(at[0], 1), round(at[1], 1)), rings, text)
            if key != previous_key:  # a held, unchanged frame is written again as it is
                box = tuple(v * per_css for v in (rect[0], rect[1], rect[0] + rect[2], rect[1] + rect[3]))
                frame = Image.frombuffer("RGB", (info.width, info.height), raw).resize((width, height), Image.BICUBIC, box=box)

                def place(x: float, y: float) -> tuple[float, float]:
                    return (x - rect[0]) / rect[2] * width, (y - rect[1]) / rect[3] * height

                for progress, cx, cy in rings:
                    px, py = place(cx, cy)
                    ring = overlays.ripple(progress, ripple_radius)
                    frame.paste(ring, (round(px - ripple_radius), round(py - ripple_radius)), ring)
                if at is not None:
                    px, py = place(*at)
                    frame.paste(halo, (round(px - halo.width / 2), round(py - halo.height / 2)), halo)
                    frame.paste(sprite, (round(px - tip), round(py - tip)), sprite)
                if text:
                    panel = overlays.caption(text, width, font_px, font_path)
                    frame.paste(panel, ((width - panel.width) // 2, height - panel.height - round(height * 0.05)), panel)
                previous_key, previous_frame = key, frame.tobytes()
            encoder.stdin.write(previous_frame)
        encoder.stdin.close()
        if encoder.wait() != 0:
            raise media.MediaError(f"ffmpeg could not encode the video: {encoder.stderr.read().decode().strip()[-500:]}")
    except BrokenPipeError as error:
        raise media.MediaError(f"ffmpeg stopped while encoding: {encoder.stderr.read().decode().strip()[-500:]}") from error
    finally:
        decoder.kill()
        decoder.wait()
        if encoder.poll() is None:
            encoder.kill()
            encoder.wait()


def _encode_command(edl: EDL, clips: dict[str, Path], out: Path, size: tuple[int, int], crf: int) -> list[str]:
    command = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{size[0]}x{size[1]}", "-r", f"{edl.fps:g}", "-i", "pipe:0",
    ]  # fmt: skip
    cues = [cue for cue in edl.audio if cue.step_id in clips]
    for cue in cues:
        command += ["-i", str(clips[cue.step_id])]
    command += ["-map", "0:v"]
    if cues:
        chains = []
        for number, cue in enumerate(cues):
            tempo = f"atempo={cue.tempo}," if cue.tempo != 1 else ""
            chains.append(f"[{number + 1}:a]{tempo}adelay={round(cue.at * 1000)}:all=1[a{number}]")
        mixed = "".join(f"[a{number}]" for number in range(len(cues)))
        chains.append(f"{mixed}amix=inputs={len(cues)}:normalize=0:dropout_transition=0,{LOUDNESS},apad[narration]")
        command += ["-filter_complex", ";".join(chains), "-map", "[narration]", "-c:a", "aac", "-b:a", "160k", "-ar", "48000"]
    command += [
        "-c:v", "libx264", "-preset", "medium", "-crf", str(crf), "-pix_fmt", "yuv420p",
        "-t", f"{edl.duration:.3f}", "-movflags", "+faststart", str(out),
    ]  # fmt: skip
    return command

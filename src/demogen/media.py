"""Thin wrappers over ffmpeg and ffprobe."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from demogen.errors import DemogenError


class MediaError(DemogenError):
    pass


def run(tool: str, *args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run([tool, "-hide_banner", *args], capture_output=True, text=True, check=True)
    except FileNotFoundError as error:
        raise MediaError(f"{tool} is not installed or not on the PATH") from error
    except subprocess.CalledProcessError as error:
        raise MediaError(f"{tool} failed: {error.stderr.strip()[-500:]}") from error


@dataclass(frozen=True)
class VideoInfo:
    seconds: float
    fps: float
    frames: int
    width: int
    height: int


def probe(video: Path) -> VideoInfo:
    out = run(
        "ffprobe", "-v", "error", "-select_streams", "v:0", "-count_packets",
        "-show_entries", "stream=width,height,r_frame_rate,nb_read_packets:format=duration", "-of", "json", str(video),
    )  # fmt: skip
    data = json.loads(out.stdout)
    stream = data["streams"][0]
    rate, _, base = stream["r_frame_rate"].partition("/")
    return VideoInfo(
        seconds=float(data["format"]["duration"]),
        fps=float(rate) / float(base or 1),
        frames=int(stream["nb_read_packets"]),
        width=stream["width"],
        height=stream["height"],
    )


def decode_audio(data: bytes, rate: int) -> np.ndarray:
    """Any audio file's contents as mono float32 samples at the given rate."""
    try:
        out = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", "pipe:0", "-ar", str(rate), "-ac", "1", "-f", "f32le", "pipe:1"],
            input=data, capture_output=True, check=True,
        )  # fmt: skip
    except FileNotFoundError as error:
        raise MediaError("ffmpeg is not installed or not on the PATH") from error
    except subprocess.CalledProcessError as error:
        raise MediaError(f"ffmpeg could not decode the audio: {error.stderr.decode().strip()[-500:]}") from error
    return np.frombuffer(out.stdout, dtype=np.float32)

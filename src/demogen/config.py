from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

Role = Literal["planner", "vision", "writer"]


class ModelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str = "litellm"
    model: str
    params: dict[str, Any] = {}


class RecordingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    headless: bool = True
    fps: int = Field(default=30, ge=1, le=60)
    crf: int = Field(default=18, ge=0, le=51)  # x264 quality; lower is better and larger
    max_heals: int = Field(default=2, ge=0)  # how often a failed take may be repaired and retried


class TTSConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str = "kokoro"
    voice: str = "af_heart"
    speed: float = Field(default=1.0, gt=0)
    params: dict[str, Any] = {}


class STTConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str = "faster_whisper"
    model: str = "base.en"
    params: dict[str, Any] = {}


class VoiceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tts: TTSConfig = TTSConfig()
    stt: STTConfig = STTConfig()
    # A clip is voiced again when speech-to-text hears it this far from its script.
    max_wer: float = Field(default=0.3, ge=0, le=1)
    max_attempts: int = Field(default=2, ge=1)


class RenderConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    height: int | None = Field(default=1080, ge=240)  # the finished video's height; None keeps the take's
    crf: int = Field(default=18, ge=0, le=51)
    zoom: float = Field(default=1.4, ge=1)  # how far to zoom in on what a step acts on; 1 turns zoom off
    pointer: bool = True
    captions: bool = True
    font: str | None = None  # a TrueType font file for captions; the built-in font if unset


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")

    auto_approve: bool = False
    max_rewrite_rounds: int = Field(default=2, ge=0)
    # How fast the voice reads, in words a second, for sizing narration before it is voiced.
    # Measured: Kokoro's default voice at speed 1.0 reads 3.2 to 4.2; 3.0 leaves a margin.
    target_wps: float = Field(default=3.0, gt=0)
    # Keyframes are scaled so neither side exceeds this before a vision model sees them.
    keyframe_max_edge: int = Field(default=1920, ge=640, le=2576)
    models: dict[Role, ModelConfig] = {}
    recording: RecordingConfig = RecordingConfig()
    voice: VoiceConfig = VoiceConfig()
    render: RenderConfig = RenderConfig()

    def fingerprint(self, keys: tuple[str, ...]) -> dict[str, Any]:
        """The slice of config a stage depends on, for its cache key. Keys are dotted paths."""
        dumped = self.model_dump(mode="json")
        out: dict[str, Any] = {}
        for key in keys:
            value: Any = dumped
            for part in key.split("."):
                value = value.get(part) if isinstance(value, dict) else None
            out[key] = value
        return out


def load_config(path: Path | None) -> Config:
    if path is None:
        return Config()
    return Config.model_validate(yaml.safe_load(path.read_text()) or {})

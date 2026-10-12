"""Speech engines are plugins: `demogen.tts` turns text into audio, `demogen.stt` hears it back."""

from __future__ import annotations

import json
import threading
from typing import Any, Protocol

from demogen.config import STTConfig, TTSConfig
from demogen.plugins import load_plugin
from demogen.voice.check import Heard


class TTSProvider(Protocol):
    def synthesize(self, text: str, *, voice: str, speed: float) -> bytes:
        """The text spoken, as a 16-bit mono WAV file."""
        ...


class STTProvider(Protocol):
    def transcribe(self, wav: bytes) -> list[Heard]:
        """The words heard in a WAV file, in order, each with its start and end time."""
        ...


# Speech models are slow to load and large in memory, so each configuration is built once.
_instances: dict[str, Any] = {}
_lock = threading.Lock()


def _instance(group: str, provider: str, **params: Any) -> Any:
    key = json.dumps([group, provider, params], sort_keys=True)
    with _lock:
        if key not in _instances:
            _instances[key] = load_plugin(group, provider)(**params)
        return _instances[key]


def tts_for(config: TTSConfig) -> TTSProvider:
    return _instance("tts", config.provider, **config.params)


def stt_for(config: STTConfig) -> STTProvider:
    return _instance("stt", config.provider, model=config.model, **config.params)

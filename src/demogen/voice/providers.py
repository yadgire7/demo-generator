"""Speech engines are plugins: `demogen.tts` turns text into audio, `demogen.stt` hears it back."""

from __future__ import annotations

import json
import threading
from typing import Any, Protocol

from demogen import telemetry
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


class _TracedTTS:
    def __init__(self, inner: TTSProvider, provider: str):
        self.inner, self.provider = inner, provider

    def synthesize(self, text: str, *, voice: str, speed: float) -> bytes:
        attributes = dict(demogen__tts__provider=self.provider, demogen__tts__voice=voice, demogen__tts__characters=len(text))
        with telemetry.span("tts.synthesize", telemetry.CLIENT, **attributes):
            return self.inner.synthesize(text, voice=voice, speed=speed)


class _TracedSTT:
    def __init__(self, inner: STTProvider, provider: str, model: str):
        self.inner, self.provider, self.model = inner, provider, model

    def transcribe(self, wav: bytes) -> list[Heard]:
        with telemetry.span("stt.transcribe", telemetry.CLIENT, demogen__stt__provider=self.provider, demogen__stt__model=self.model) as span:
            words = self.inner.transcribe(wav)
            span.set_attribute("demogen.stt.words", len(words))
            return words


def tts_for(config: TTSConfig) -> TTSProvider:
    return _TracedTTS(_instance("tts", config.provider, **config.params), config.provider)


def stt_for(config: STTConfig) -> STTProvider:
    return _TracedSTT(_instance("stt", config.provider, model=config.model, **config.params), config.provider, config.model)

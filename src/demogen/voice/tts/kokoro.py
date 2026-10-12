from __future__ import annotations

import threading

import numpy as np

from demogen.errors import DemogenError
from demogen.voice.audio import float_to_wav

SAMPLE_RATE = 24000


class KokoroTTS:
    """Kokoro-82M: a small, Apache-licensed voice model that runs on a CPU. Voices are presets."""

    def __init__(self, repo_id: str = "hexgrad/Kokoro-82M"):
        self.repo_id = repo_id
        self._pipelines: dict[str, object] = {}
        self._lock = threading.Lock()  # the model is shared and not safe to call from two threads

    def _pipeline(self, language: str):
        if language not in self._pipelines:
            try:
                from kokoro import KPipeline
            except ImportError as error:
                raise DemogenError("Kokoro is not installed. Install it with: pip install 'demogen[voice]'") from error
            self._pipelines[language] = KPipeline(lang_code=language, repo_id=self.repo_id)
        return self._pipelines[language]

    def synthesize(self, text: str, *, voice: str, speed: float) -> bytes:
        with self._lock:
            # A voice's first letter names its language: af_heart is American, bf_emma British.
            chunks = [np.asarray(audio) for _, _, audio in self._pipeline(voice[0])(text, voice=voice, speed=speed)]
        if not chunks:
            raise DemogenError(f"Kokoro produced no audio for: {text!r}")
        return float_to_wav(np.concatenate(chunks), SAMPLE_RATE)

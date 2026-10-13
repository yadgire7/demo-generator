from __future__ import annotations

import threading

import numpy as np

from demogen import media
from demogen.errors import DemogenError
from demogen.voice.check import Heard

WHISPER_RATE = 16000
TAIL_SILENCE_S = 0.6


class FasterWhisperSTT:
    """Whisper through CTranslate2 (MIT). Small English models are quick on a CPU."""

    def __init__(self, model: str = "base.en", device: str = "cpu", compute_type: str = "int8", language: str = "en"):
        try:
            from faster_whisper import WhisperModel
        except ImportError as error:
            raise DemogenError("faster-whisper is not installed. Install it with: pip install 'demogen[voice]'") from error
        self.language = language
        self._model = WhisperModel(model, device=device, compute_type=compute_type)
        self._lock = threading.Lock()

    def transcribe(self, wav: bytes) -> list[Heard]:
        samples = media.decode_audio(wav, WHISPER_RATE)
        # Whisper tends to repeat the last word of a clip that ends right after it. A stretch of
        # silence on the end stops that, and changes no timings.
        samples = np.concatenate([samples, np.zeros(round(TAIL_SILENCE_S * WHISPER_RATE), dtype=np.float32)])
        with self._lock:
            segments, _ = self._model.transcribe(samples, word_timestamps=True, language=self.language, condition_on_previous_text=False)
            pieces = [word for segment in segments for word in segment.words]
        words: list[Heard] = []
        for piece in pieces:
            # Whisper starts each new word with a space; a piece without one continues the last
            # word ("$1" + ",200").
            if words and not piece.word.startswith(" "):
                words[-1] = Heard(words[-1].w + piece.word, words[-1].start, float(piece.end))
            else:
                words.append(Heard(piece.word.strip(), float(piece.start), float(piece.end)))
        return words

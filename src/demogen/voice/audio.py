"""Narration clips: 16-bit mono WAV, trimmed to the speech itself."""

from __future__ import annotations

import io
import wave

import numpy as np

# Samples quieter than this share of the clip's peak count as silence.
SILENCE_RATIO = 0.01
# Kept around the speech so words are not clipped and the clip does not end abruptly.
LEAD_PAD_S = 0.04
TAIL_PAD_S = 0.12


def read_wav(data: bytes) -> tuple[np.ndarray, int]:
    """Samples (int16, mono) and sample rate of a WAV file."""
    with wave.open(io.BytesIO(data), "rb") as wav:
        if wav.getsampwidth() != 2:
            raise ValueError("expected 16-bit PCM audio")
        samples = np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2")
        if wav.getnchannels() > 1:
            samples = samples.reshape(-1, wav.getnchannels()).mean(axis=1).astype("<i2")
        return samples, wav.getframerate()


def write_wav(samples: np.ndarray, rate: int) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(np.asarray(samples, dtype="<i2").tobytes())
    return buffer.getvalue()


def float_to_wav(samples: np.ndarray, rate: int) -> bytes:
    """Encode floating-point samples in [-1, 1], as speech models produce them."""
    return write_wav((np.clip(samples, -1.0, 1.0) * 32767).astype("<i2"), rate)


def trim_silence(samples: np.ndarray, rate: int) -> np.ndarray:
    """Cut the silence before and after the speech, leaving a short pad at each end."""
    level = np.abs(samples.astype(np.int32))
    if level.size == 0 or level.max() == 0:
        return samples
    loud = np.nonzero(level > level.max() * SILENCE_RATIO)[0]
    start = max(0, loud[0] - round(LEAD_PAD_S * rate))
    end = min(len(samples), loud[-1] + 1 + round(TAIL_PAD_S * rate))
    return samples[start:end]

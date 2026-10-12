"""How much narration a step's footage can carry."""

from __future__ import annotations

import math
from dataclasses import dataclass

# Narration may run this much longer than its step's footage: the editor holds a frame to
# cover the difference. Beyond it, the passage is rewritten instead.
MAX_OVERRUN = 1.6
# The writer is held a little inside that line, because real speech pace varies.
WRITER_HEADROOM = 0.9


def word_budget(window_s: float, wps: float) -> int:
    """How many words fit a step's on-screen window at the target pace. Never below one."""
    return max(1, math.floor(window_s * wps))


def speech_seconds(text: str, wps: float) -> float:
    return len(text.split()) / wps


@dataclass(frozen=True)
class Budget:
    seconds: float  # how long the step is on screen
    target: int  # words that fit that time
    limit: int  # words beyond which the passage will not fit even with a held frame

    @classmethod
    def for_window(cls, seconds: float, wps: float) -> Budget:
        target = word_budget(seconds, wps)
        return cls(seconds, target, max(target, word_budget(seconds * MAX_OVERRUN * WRITER_HEADROOM, wps)))

    def tightened(self, words_spoken: int, seconds_spoken: float) -> Budget:
        """The budget for a passage that was voiced and still ran too long, at the pace it was spoken."""
        fits = math.floor(words_spoken * self.seconds * MAX_OVERRUN * WRITER_HEADROOM / seconds_spoken)
        limit = max(1, min(fits, words_spoken - 1))
        return Budget(self.seconds, min(self.target, limit), limit)

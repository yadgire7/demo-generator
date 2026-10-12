"""Compare what a clip says with what it was meant to say, and time the script's words."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

_NOT_WORD = re.compile(r"[^a-z0-9']+")


@dataclass(frozen=True)
class Heard:
    """A word as speech-to-text heard it, with its time in the clip."""

    w: str
    start: float
    end: float


@dataclass(frozen=True)
class Check:
    wer: float  # word error rate against the script: 0 is a perfect reading
    missing_start: bool  # the script's first word was not heard: the clip may start late
    missing_end: bool  # the script's last word was not heard: the clip may be cut off
    words: list[dict[str, Any]]  # the script's own words, each with a start and end time
    heard: str

    def ok(self, max_wer: float) -> bool:
        return self.wer <= max_wer and not self.missing_start and not self.missing_end


def normalize(word: str) -> str:
    return _NOT_WORD.sub("", word.lower())


def align(reference: list[str], heard: list[str]) -> list[tuple[int | None, int | None]]:
    """Pair up two word lists by minimum edit distance. `None` marks a word with no partner."""
    rows, cols = len(reference) + 1, len(heard) + 1
    cost = [[0] * cols for _ in range(rows)]
    for i in range(rows):
        cost[i][0] = i
    for j in range(cols):
        cost[0][j] = j
    for i in range(1, rows):
        for j in range(1, cols):
            swap = cost[i - 1][j - 1] + (reference[i - 1] != heard[j - 1])
            cost[i][j] = min(swap, cost[i - 1][j] + 1, cost[i][j - 1] + 1)
    pairs: list[tuple[int | None, int | None]] = []
    i, j = len(reference), len(heard)
    while i or j:
        if i and j and cost[i][j] == cost[i - 1][j - 1] + (reference[i - 1] != heard[j - 1]):
            pairs.append((i - 1, j - 1))
            i, j = i - 1, j - 1
        elif i and cost[i][j] == cost[i - 1][j] + 1:
            pairs.append((i - 1, None))
            i -= 1
        else:
            pairs.append((None, j - 1))
            j -= 1
    return pairs[::-1]


def check_clip(text: str, heard: list[Heard], duration: float) -> Check:
    script = text.split()
    # Marks that are not spoken ("-", "&") take no part in the comparison.
    spoken = [(index, normalize(word)) for index, word in enumerate(script) if normalize(word)]
    pairs = align([word for _, word in spoken], [normalize(h.w) for h in heard])

    errors = 0
    times: dict[int, tuple[float, float]] = {}
    unheard: set[int] = set()
    for ref, hyp in pairs:
        if ref is None or hyp is None:
            errors += 1
            if ref is not None:
                unheard.add(ref)
            continue
        errors += spoken[ref][1] != normalize(heard[hyp].w)
        times[spoken[ref][0]] = (heard[hyp].start, heard[hyp].end)

    return Check(
        wer=errors / len(spoken) if spoken else 0.0,
        missing_start=bool(spoken) and 0 in unheard,
        missing_end=bool(spoken) and len(spoken) - 1 in unheard,
        words=_timed(script, times, duration),
        heard=" ".join(h.w for h in heard),
    )


def _timed(script: list[str], times: dict[int, tuple[float, float]], duration: float) -> list[dict[str, Any]]:
    """Every script word with a time: its own if it was heard, otherwise a share of the gap it sits in."""
    words = []
    index = 0
    while index < len(script):
        if index in times:
            start, end = times[index]
            words.append({"w": script[index], "start": round(start, 3), "end": round(end, 3)})
            index += 1
            continue
        run_end = index
        while run_end < len(script) and run_end not in times:
            run_end += 1
        gap_start = words[-1]["end"] if words else 0.0
        gap_end = times[run_end][0] if run_end < len(script) else duration
        share = max(gap_end - gap_start, 0.0) / (run_end - index)
        for k in range(index, run_end):
            offset = gap_start + (k - index) * share
            words.append({"w": script[k], "start": round(offset, 3), "end": round(offset + share, 3)})
        index = run_end
    return words

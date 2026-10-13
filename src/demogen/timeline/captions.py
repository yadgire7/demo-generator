"""Captions: the narration's words grouped into short cues, timed to when each is spoken."""

from __future__ import annotations

from demogen.timeline.edl import Caption

MAX_CUE_CHARS = 64  # about two comfortable lines
LINGER_S = 0.25  # a cue stays up briefly after its last word
MIN_GAP_S = 0.02

_SENTENCE_END = (".", "!", "?")


def build_cues(words: list[tuple[str, float, float]]) -> list[Caption]:
    """Group timed words (text, start, end) into cues, breaking at sentence ends and at length."""
    groups: list[list[tuple[str, float, float]]] = []
    current: list[tuple[str, float, float]] = []
    for word in words:
        if current and len(" ".join(w for w, _, _ in current)) + 1 + len(word[0]) > MAX_CUE_CHARS:
            groups.append(current)
            current = []
        current.append(word)
        if word[0].endswith(_SENTENCE_END):
            groups.append(current)
            current = []
    if current:
        groups.append(current)

    cues = []
    for index, group in enumerate(groups):
        end = group[-1][2] + LINGER_S
        if index + 1 < len(groups):
            end = min(end, groups[index + 1][0][1] - MIN_GAP_S)
        end = max(end, group[-1][2])
        cues.append(Caption(start=round(group[0][1], 3), end=round(end, 3), text=" ".join(w for w, _, _ in group)))
    return cues


def _stamp(seconds: float) -> str:
    millis = round(seconds * 1000)
    hours, millis = divmod(millis, 3_600_000)
    minutes, millis = divmod(millis, 60_000)
    secs, millis = divmod(millis, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def to_srt(captions: list[Caption]) -> str:
    return "".join(f"{n}\n{_stamp(c.start)} --> {_stamp(c.end)}\n{c.text}\n\n" for n, c in enumerate(captions, start=1))

"""The edit decision list: everything the renderer needs to turn a take into the finished video.

Times are seconds. "Source" times are on the recorded take's clock, "output" times on the
finished video's. Positions are in the page's CSS pixels, as the take's events are.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from demogen.store.artifacts import ArtifactRef


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Segment(_Model):
    """Play the take from `source_start` to `source_end`, then hold its last frame for `hold`."""

    source_start: float
    source_end: float
    at: float  # output time at which the segment starts
    hold: float = 0.0

    @property
    def end(self) -> float:
        return self.at + (self.source_end - self.source_start) + self.hold


class AudioCue(_Model):
    step_id: str
    clip: ArtifactRef | None
    at: float
    tempo: float  # playback speed; above 1 shortens the clip
    duration: float  # how long it runs in the output, after the tempo change


class ZoomKey(_Model):
    """At output time `t` the picture shows this rectangle of the page: x, y, width, height."""

    t: float
    rect: tuple[float, float, float, float]


class Caption(_Model):
    start: float
    end: float
    text: str


class StepSpan(_Model):
    step_id: str
    start: float
    end: float


class EDL(_Model):
    fps: float
    viewport: tuple[int, int]
    duration: float
    segments: list[Segment]
    audio: list[AudioCue]
    zoom: list[ZoomKey]
    captions: list[Caption]
    steps: list[StepSpan]

    def to_output(self, source_t: float) -> float:
        """Where a moment of the take lands in the output. A moment that was cut lands at the cut."""
        for segment in self.segments:
            if source_t <= segment.source_end:
                return segment.at + max(source_t - segment.source_start, 0.0)
        return self.duration

    def source_at(self, output_t: float) -> float:
        """Which moment of the take is on screen at an output time. During a hold, the held frame."""
        for segment in self.segments:
            if output_t < segment.end:
                played = min(max(output_t - segment.at, 0.0), segment.source_end - segment.source_start)
                return segment.source_start + played
        return self.segments[-1].source_end

    def rect_at(self, output_t: float) -> tuple[float, float, float, float]:
        """The part of the page in view at an output time, easing between zoom keys."""
        full = (0.0, 0.0, float(self.viewport[0]), float(self.viewport[1]))
        if not self.zoom or output_t <= self.zoom[0].t:
            return self.zoom[0].rect if self.zoom else full
        for before, after in zip(self.zoom, self.zoom[1:]):
            if output_t <= after.t:
                p = (output_t - before.t) / (after.t - before.t) if after.t > before.t else 1.0
                p = p * p * (3 - 2 * p)  # ease in and out
                return tuple(a + (b - a) * p for a, b in zip(before.rect, after.rect))
        return self.zoom[-1].rect

"""Pointer movement that reads as a person's hand: a gentle arc that eases in and out."""

from __future__ import annotations

import math
import random

Point = tuple[float, float]

SAMPLE_HZ = 60
MIN_SECONDS, MAX_SECONDS = 0.3, 0.9
PIXELS_PER_SECOND = 1500  # longer journeys take longer, up to MAX_SECONDS
MAX_BOW = 0.12  # how far the arc may bow sideways, as a fraction of the distance


def move_seconds(start: Point, end: Point) -> float:
    return min(MAX_SECONDS, max(MIN_SECONDS, 0.25 + math.dist(start, end) / PIXELS_PER_SECOND))


def mouse_path(start: Point, end: Point, rng: random.Random) -> list[Point]:
    """Points from just after `start` to exactly `end`, one per display frame."""
    if math.dist(start, end) < 1:
        return [end]
    dx, dy = end[0] - start[0], end[1] - start[1]
    bow = rng.uniform(-MAX_BOW, MAX_BOW)
    # A quadratic Bézier whose control point sits off the straight line, at its midpoint.
    control = (start[0] + dx / 2 - dy * bow, start[1] + dy / 2 + dx * bow)
    samples = max(2, round(move_seconds(start, end) * SAMPLE_HZ))
    path = []
    for i in range(1, samples + 1):
        t = i / samples
        t = t * t * (3 - 2 * t)  # smoothstep: slow, fast, slow
        a, b, c = (1 - t) ** 2, 2 * (1 - t) * t, t * t
        path.append((a * start[0] + b * control[0] + c * end[0], a * start[1] + b * control[1] + c * end[1]))
    path[-1] = end
    return path

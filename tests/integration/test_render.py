"""The compositor on a synthetic take: a plain white video, so every overlay can be found by colour."""

from __future__ import annotations

import io
import subprocess

import numpy as np
import pytest
from PIL import Image

from demogen import media
from demogen.editor.compositor import output_size, render_video
from demogen.editor.overlays import ACCENT
from demogen.timeline.edl import EDL, AudioCue, Caption, Segment, ZoomKey
from demogen.voice.audio import float_to_wav

W, H = 640, 400  # the take, and the page: one source pixel per CSS pixel


@pytest.fixture(scope="module")
def take(tmp_path_factory):
    """Three seconds of white with a black square at the page's centre for its second half."""
    path = tmp_path_factory.mktemp("take") / "raw.mp4"
    square = "drawbox=x=300:y=180:w=40:h=40:color=black:t=fill:enable='gte(t,1.5)'"
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", f"color=c=white:s={W}x{H}:r=30:d=3", "-vf", square, "-pix_fmt", "yuv420p", str(path)],
        check=True,
    )  # fmt: skip
    return path


@pytest.fixture
def tone(tmp_path):
    path = tmp_path / "tone.wav"
    t = np.arange(24000) / 24000
    path.write_bytes(float_to_wav((0.4 * np.sin(2 * np.pi * 330 * t)).astype(np.float32), 24000))
    return path


def _edl(**fields) -> EDL:
    base = dict(fps=30, viewport=(W, H), duration=3.0, segments=[Segment(source_start=0, source_end=3, at=0)], audio=[], zoom=[], captions=[], steps=[])
    return EDL(**{**base, **fields})


def _frame(video, t) -> np.ndarray:
    png = subprocess.run(["ffmpeg", "-loglevel", "error", "-ss", f"{t}", "-i", str(video), "-frames:v", "1", "-f", "image2pipe", "-c:v", "png", "-"], capture_output=True, check=True).stdout
    return np.asarray(Image.open(io.BytesIO(png)).convert("RGB")).astype(int)


def _is(pixel, colour, tolerance=40) -> bool:
    return all(abs(int(p) - c) <= tolerance for p, c in zip(pixel, colour))


WHITE, BLACK = (255, 255, 255), (0, 0, 0)


def test_output_size_keeps_the_shape_and_even_sides():
    source = media.VideoInfo(seconds=1, fps=30, frames=30, width=2880, height=1800)
    assert output_size(source, 1080) == (1728, 1080)
    assert output_size(source, None) == (2880, 1800) and output_size(source, 4000) == (2880, 1800)  # never scaled up
    assert output_size(source, 721) == (1152, 720)


def test_plain_render_matches_the_take(take, tmp_path):
    out = tmp_path / "final.mp4"
    render_video(take, _edl(), [], {}, out, height=None, crf=18)
    info = media.probe(out)
    assert (info.width, info.height, info.fps) == (W, H, 30) and info.seconds == pytest.approx(3.0, abs=0.04)
    assert _is(_frame(out, 1.0)[200, 320], WHITE) and _is(_frame(out, 2.0)[200, 320], BLACK)


def test_a_held_frame_stretches_the_video_and_delays_what_follows(take, tmp_path):
    # Hold the frame at 1.0 s (still white) for two seconds; the square, due at 1.5 s, comes 2 s later.
    segments = [Segment(source_start=0, source_end=1.0, at=0, hold=2.0), Segment(source_start=1.0, source_end=3.0, at=3.0)]
    out = tmp_path / "final.mp4"
    render_video(take, _edl(duration=5.0, segments=segments), [], {}, out, height=None, crf=18)
    assert media.probe(out).seconds == pytest.approx(5.0, abs=0.04)
    assert _is(_frame(out, 2.0)[200, 320], WHITE) and _is(_frame(out, 3.3)[200, 320], WHITE)
    assert _is(_frame(out, 3.7)[200, 320], BLACK)


def test_pointer_follows_its_track_and_clicks_ripple(take, tmp_path):
    events = [
        {"t": 0.0, "type": "cursor", "x": 100, "y": 100},
        {"t": 1.0, "type": "cursor", "x": 500, "y": 300},
        {"t": 1.0, "type": "click", "x": 500, "y": 300},
    ]
    out = tmp_path / "final.mp4"
    render_video(take, _edl(), events, {}, out, height=None, crf=12, captions=False)

    def halo_at(t, x, y):
        # The halo is a translucent disc of the accent colour around the pointer's tip.
        return _is(_frame(out, t)[y - 6, x - 6], tuple((a * 80 + 255 * 175) // 255 for a in ACCENT), tolerance=30)

    assert halo_at(0.0, 100, 100) and not halo_at(0.0, 500, 300)
    assert halo_at(0.5, 300, 200)  # halfway along
    assert halo_at(2.0, 500, 300) and not halo_at(2.0, 100, 100)  # and it stays where it stopped
    # The arrow itself is dark, just below and right of its tip.
    assert _frame(out, 2.0)[300 + 6, 500 + 2].max() < 90

    def ring_pixels(t):
        """How much of the area around the click differs from how it looks once the ripple is over."""
        region = (slice(300 - 40, 300 + 40), slice(500 - 40, 500 + 40))
        return int((np.abs(_frame(out, t)[region] - _frame(out, 2.5)[region]).sum(axis=2) > 60).sum())

    assert ring_pixels(1.2) > 40  # a ring is spreading out from the click
    assert ring_pixels(1.6) == 0  # and has faded away

    hidden = tmp_path / "hidden.mp4"
    render_video(take, _edl(), events, {}, hidden, height=None, crf=12, pointer=False, captions=False)
    assert _is(_frame(hidden, 0.0)[94, 94], WHITE)


def test_zoom_enlarges_the_part_of_the_page_in_view(take, tmp_path):
    # Zoom to the centre quarter: the 40 px square fills twice as much of the frame.
    zoom = [ZoomKey(t=0.0, rect=(0, 0, W, H)), ZoomKey(t=1.0, rect=(160, 100, 320, 200)), ZoomKey(t=3.0, rect=(160, 100, 320, 200))]
    out = tmp_path / "final.mp4"
    events = [{"t": 0.0, "type": "cursor", "x": 200, "y": 150}]
    render_video(take, _edl(zoom=zoom), events, {}, out, height=None, crf=12, captions=False)
    frame = _frame(out, 2.5)
    dark = np.argwhere(frame[:, 150:].max(axis=2) < 60) + (0, 150)  # right of the pointer, which is dark too
    (top, left), (bottom, right) = dark.min(axis=0), dark.max(axis=0)
    assert (right - left, bottom - top) == (pytest.approx(80, abs=3), pytest.approx(80, abs=3))
    assert ((left + right) / 2, (top + bottom) / 2) == (pytest.approx(320, abs=3), pytest.approx(200, abs=3))
    # The pointer, at page (200, 150), moves with the page: now a quarter of the way in, not at (200, 150).
    disc = tuple((a * 80 + 255 * 175) // 255 for a in ACCENT)
    assert _is(frame[100 - 6, 80 - 6], disc, tolerance=30) and _is(frame[150 - 6, 200 - 6], WHITE)


def test_captions_show_only_during_their_cue(take, tmp_path):
    out = tmp_path / "final.mp4"
    render_video(take, _edl(captions=[Caption(start=0.5, end=1.2, text="One click, and it is done.")]), [], {}, out, height=None, crf=12)

    def panel(t):
        band = _frame(out, t)[H - 60 : H - 15]
        return int((band.max(axis=2) < 120).sum()), int((band.min(axis=2) > 200).sum())

    dark, light = panel(0.8)
    assert dark > 3000 and light > 300  # a dark panel with light text on it
    assert panel(0.2)[0] == 0 and panel(2.0)[0] == 0

    off = tmp_path / "off.mp4"
    render_video(take, _edl(captions=[Caption(start=0.5, end=1.2, text="Hidden")]), [], {}, off, height=None, crf=12, captions=False)
    assert int((_frame(off, 0.8)[H - 60 : H - 15].max(axis=2) < 120).sum()) == 0


def test_narration_is_mixed_in_at_its_cue_and_fills_the_video(take, tone, tmp_path):
    cue = AudioCue(step_id="a", clip={"path": "tone.wav", "sha256": "x"}, at=1.0, tempo=1.0, duration=1.0)
    out = tmp_path / "final.mp4"
    render_video(take, _edl(audio=[cue]), [], {"a": tone}, out, height=None, crf=18)
    sound = media.decode_audio(media.extract_wav(out), 16000)
    assert len(sound) / 16000 == pytest.approx(3.0, abs=0.06)  # padded to the video's full length

    def level(start, end):
        return float(np.abs(sound[round(start * 16000) : round(end * 16000)]).mean())

    assert level(0.1, 0.9) < 0.002 and level(2.3, 2.9) < 0.002
    assert level(1.2, 1.8) > 0.05


def test_tempo_shortens_a_clip(take, tone, tmp_path):
    cue = AudioCue(step_id="a", clip={"path": "tone.wav", "sha256": "x"}, at=0.5, tempo=1.25, duration=0.8)
    out = tmp_path / "final.mp4"
    render_video(take, _edl(audio=[cue]), [], {"a": tone}, out, height=None, crf=18)
    sound = media.decode_audio(media.extract_wav(out), 16000)
    loud = np.nonzero(np.abs(sound) > 0.02)[0]
    assert loud[0] / 16000 == pytest.approx(0.5, abs=0.05) and loud[-1] / 16000 == pytest.approx(1.3, abs=0.06)


def test_output_is_scaled_to_the_requested_height(take, tmp_path):
    out = tmp_path / "final.mp4"
    render_video(take, _edl(), [], {}, out, height=200, crf=18)
    info = media.probe(out)
    assert (info.width, info.height) == (320, 200)

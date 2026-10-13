"""The timeline planner: fitting narration to footage, holds, zoom and captions. All pure."""

import pytest

from demogen.timeline.captions import build_cues, to_srt
from demogen.timeline.edl import EDL, Caption, Segment, ZoomKey
from demogen.timeline.planner import GAP_S, LEAD_S, fit, plan_timeline

VIEWPORT = (1440, 900)


@pytest.mark.parametrize(
    "clip,window,expected",
    [
        (2.0, 4.0, (1.0, 0.0)),  # fits with room to spare
        (3.7, 4.0, (1.0, 0.0)),  # exactly the room there is: 4.0 less the lead and the gap
        (4.0, 4.0, (pytest.approx(1.0811, abs=1e-4), 0.0)),  # a little long: speed it up, at most 10%
        (5.0, 4.0, (1.0, pytest.approx(1.3))),  # too long to speed up: hold the last frame for the rest
        (2.0, 0.2, (1.0, pytest.approx(2.1))),  # a step shorter than the breathing room itself
    ],
)
def test_fit(clip, window, expected):
    assert fit(clip, window) == expected


def _plan(clips, windows=None, events=(), words=None, zoom=1.0, video_seconds=10.0):
    windows = windows or {"a": {"start": 1.0, "end": 4.0}, "b": {"start": 4.0, "end": 6.0}}
    clips = {step_id: {"audio": {"path": f"audio/{step_id}.wav", "sha256": "x"}, "duration": d} for step_id, d in clips.items()}
    return plan_timeline(windows=windows, clips=clips, words=words or {}, events=list(events), video_seconds=video_seconds, fps=30, viewport=VIEWPORT, zoom=zoom)


def test_narration_that_fits_leaves_the_take_untouched():
    edl, flagged = _plan({"a": 2.0, "b": 1.0})
    assert flagged == [] and edl.duration == 10.0
    assert [(s.source_start, s.source_end, s.at, s.hold) for s in edl.segments] == [(0.0, 4.0, 0.0, 0.0), (4.0, 6.0, 4.0, 0.0), (6.0, 10.0, 6.0, 0.0)]
    assert [(c.step_id, c.at, c.tempo, c.duration) for c in edl.audio] == [("a", 1.0 + LEAD_S, 1.0, 2.0), ("b", 4.0 + LEAD_S, 1.0, 1.0)]
    assert [(s.step_id, s.start, s.end) for s in edl.steps] == [("a", 1.0, 4.0), ("b", 4.0, 6.0)]


def test_long_narration_holds_the_steps_last_frame_and_pushes_everything_after_it():
    edl, flagged = _plan({"a": 4.0, "b": 1.0})  # `a` has 2.7 s of room: 1.3 s over
    first, second, tail = edl.segments
    assert first.hold == pytest.approx(1.3) and (second.at, tail.at) == (pytest.approx(5.3), pytest.approx(7.3))
    assert edl.duration == pytest.approx(11.3) and flagged == []
    assert edl.audio[1].at == pytest.approx(5.3 + LEAD_S)  # the next clip starts after the hold
    assert edl.steps[0].end == pytest.approx(5.3)
    # The clip ends a breath before the next step begins.
    assert edl.audio[0].at + edl.audio[0].duration == pytest.approx(edl.steps[0].end - GAP_S)


def test_slightly_long_narration_is_sped_up_instead():
    edl, _ = _plan({"a": 2.9, "b": 1.0})  # 2.7 s of room: 7% over
    assert edl.segments[0].hold == 0 and edl.audio[0].tempo == pytest.approx(2.9 / 2.7, abs=1e-3)
    assert edl.audio[0].duration == pytest.approx(2.7, abs=0.01) and edl.duration == 10.0


def test_narration_far_too_long_is_flagged_for_a_rewrite_but_still_planned():
    edl, flagged = _plan({"a": 2.0, "b": 3.3})  # `b` is on screen for 2.0 s: more than 1.6 times over
    assert flagged == ["b"] and edl.segments[1].hold == pytest.approx(1.6)


def test_time_maps_between_take_and_output_across_a_hold():
    edl, _ = _plan({"a": 4.0, "b": 1.0})
    assert edl.to_output(3.0) == 3.0 and edl.to_output(4.0) == 4.0  # before and at the held frame
    assert edl.to_output(4.5) == pytest.approx(5.8)  # after it, everything is 1.3 s later
    assert edl.source_at(4.6) == 4.0 and edl.source_at(5.2) == 4.0  # during the hold: the held frame
    assert edl.source_at(5.8) == pytest.approx(4.5) and edl.source_at(99) == 10.0


def test_caption_times_follow_the_clip_and_its_tempo():
    words = {"a": [{"w": "Add", "start": 0.0, "end": 0.5}, {"w": "it.", "start": 0.5, "end": 1.45}], "b": []}
    normal, _ = _plan({"a": 1.45, "b": 1.0}, words=words)
    assert [(c.text, c.start, c.end) for c in normal.captions] == [("Add it.", 1.1, 2.8)]
    fast, _ = _plan({"a": 2.9, "b": 1.0}, words=words)  # sped up by 2.9 / 2.7
    assert fast.captions[0].start == 1.1 and fast.captions[0].end == pytest.approx(1.1 + 1.45 / (2.9 / 2.7) + 0.25, abs=0.01)


def _action(t, step_id, x, y, w=100, h=40, action="click"):
    return {"t": t, "type": "action", "step_id": step_id, "action": action, "bbox": {"x": x, "y": y, "width": w, "height": h}}


def _settled(t, step_id):
    return {"t": t, "type": "settled", "step_id": step_id}


def test_zoom_goes_in_on_what_a_step_acts_on_and_out_once_it_has_settled():
    events = [_action(2.0, "a", 600, 400), _settled(3.2, "a")]
    edl, _ = _plan({"a": 1.0, "b": 1.0}, events=events, zoom=1.5)
    full, close = (0.0, 0.0, 1440.0, 900.0), (170.0, 120.0, 960.0, 600.0)  # centred on the element at (650, 420)
    assert [(k.t, k.rect) for k in edl.zoom] == [(0.0, full), (1.4, full), (1.9, close), (3.2, close), (3.7, full)]
    assert edl.rect_at(1.0) == full and edl.rect_at(2.5) == close
    midway = edl.rect_at(1.65)
    assert midway == pytest.approx((85.0, 60.0, 1200.0, 750.0))  # halfway in, eased


def test_zoom_view_stays_inside_the_page():
    edl, _ = _plan({"a": 1.0, "b": 1.0}, events=[_action(2.0, "a", 5, 5), _settled(3.2, "a")], zoom=1.5)
    assert edl.zoom[2].rect[:2] == (0.0, 0.0)
    edl, _ = _plan({"a": 1.0, "b": 1.0}, events=[_action(2.0, "a", 1330, 850), _settled(3.2, "a")], zoom=1.5)
    assert edl.zoom[2].rect[:2] == (480.0, 300.0)


def test_zoom_is_skipped_when_it_would_not_help():
    cases = {
        "off": dict(events=[_action(2.0, "a", 600, 400), _settled(3.2, "a")], zoom=1.0),
        "elements too far apart to frame": dict(events=[_action(1.5, "a", 10, 10), _action(2.5, "a", 1300, 800), _settled(3.2, "a")], zoom=1.5),
        "over too quickly": dict(events=[_action(2.0, "a", 600, 400), _settled(2.4, "a")], zoom=1.5),
        "nothing to point at": dict(events=[{"t": 2.0, "type": "action", "step_id": "a", "action": "press"}, _settled(3.2, "a")], zoom=1.5),
    }
    for reason, case in cases.items():
        assert _plan({"a": 1.0, "b": 1.0}, **case)[0].zoom == [], reason


def test_zoom_keys_never_run_backwards_and_land_after_holds():
    events = [_action(1.2, "a", 600, 400), _settled(3.9, "a"), _action(4.05, "b", 100, 100), _settled(5.2, "b")]
    edl, _ = _plan({"a": 4.0, "b": 1.0}, events=events, zoom=1.5)  # `a` is held for 1.3 s
    times = [k.t for k in edl.zoom]
    assert times == sorted(times)
    assert edl.zoom[4].t == pytest.approx(4.4)  # `a` is back to full view before its held frame ends
    assert edl.zoom[5].t == pytest.approx(5.3)  # `b` zooms in when it starts, after the hold


def _edl(**fields):
    base = dict(fps=30, viewport=VIEWPORT, duration=5.0, segments=[Segment(source_start=0, source_end=5, at=0)], audio=[], zoom=[], captions=[], steps=[])
    return EDL(**{**base, **fields})


def test_rect_without_zoom_is_the_whole_page():
    assert _edl().rect_at(2.0) == (0.0, 0.0, 1440.0, 900.0)
    assert _edl(zoom=[ZoomKey(t=1.0, rect=(0, 0, 720, 450))]).rect_at(0.5) == (0, 0, 720, 450)


def _words(text, start=0.0, each=0.3):
    return [(word, round(start + i * each, 2), round(start + (i + 1) * each, 2)) for i, word in enumerate(text.split())]


def test_cues_break_at_sentence_ends():
    cues = build_cues(_words("Type it. Then press Enter."))
    assert [(c.text, c.start, c.end) for c in cues] == [("Type it.", 0.0, 0.6), ("Then press Enter.", 0.6, 1.75)]  # a cue never outlasts the next one's start


def test_long_sentences_are_split_to_a_readable_length():
    text = "This sentence simply keeps on going well past what anyone could read in a single caption on screen"
    cues = build_cues(_words(text))
    assert len(cues) == 2 and all(len(c.text) <= 64 for c in cues)
    assert " ".join(c.text for c in cues) == text and cues[0].end <= cues[1].start


def test_srt_format():
    srt = to_srt([Caption(start=0.941, end=2.5, text="Type it."), Caption(start=3661.2, end=3662.0, text="Done.")])
    assert srt == "1\n00:00:00,941 --> 00:00:02,500\nType it.\n\n2\n01:01:01,200 --> 01:01:02,000\nDone.\n\n"

"""Clip trimming, the script-against-speech check, and the voicing stage's retry logic."""

import numpy as np
import pytest

from demogen.config import Config
from demogen.plugins import register_plugin
from demogen.stages import REGISTRY, StageContext
from demogen.store.artifacts import LocalArtifactStore
from demogen.voice import providers
from demogen.voice.audio import LEAD_PAD_S, TAIL_PAD_S, float_to_wav, read_wav, trim_silence, write_wav
from demogen.voice.check import Heard, align, check_clip, normalize

RATE = 24000


def _tone(seconds, lead=0.0, tail=0.0, amplitude=0.5):
    """A tone with silence before and after: the shape of a clip from a speech model."""
    t = np.arange(round(seconds * RATE)) / RATE
    signal = amplitude * np.sin(2 * np.pi * 220 * t)
    return np.concatenate([np.zeros(round(lead * RATE)), signal, np.zeros(round(tail * RATE))]).astype(np.float32)


def test_wav_round_trip():
    samples, rate = read_wav(float_to_wav(_tone(0.5), RATE))
    assert rate == RATE and len(samples) == RATE // 2 and samples.dtype.kind == "i"
    assert abs(int(np.abs(samples).max()) - 16383) < 200
    assert read_wav(write_wav(samples, rate))[0].tolist() == samples.tolist()


def test_trim_removes_edge_silence_but_keeps_a_pad():
    samples, rate = read_wav(float_to_wav(_tone(1.0, lead=0.3, tail=0.45), RATE))
    trimmed = trim_silence(samples, rate)
    assert len(trimmed) / rate == pytest.approx(1.0 + LEAD_PAD_S + TAIL_PAD_S, abs=0.01)
    # Already tight, or pure silence: nothing to cut.
    assert len(trim_silence(trimmed, rate)) == len(trimmed)
    silence = np.zeros(1000, dtype="<i2")
    assert len(trim_silence(silence, rate)) == 1000


def test_normalize_ignores_case_and_punctuation():
    assert [normalize(w) for w in ["Done.", "$1,200", "that's", "—", "ACME"]] == ["done", "1200", "that's", "", "acme"]


def test_align_pairs_words_and_marks_the_unpaired():
    assert align(["a", "b", "c"], ["a", "c"]) == [(0, 0), (1, None), (2, 1)]
    assert align(["a", "c"], ["a", "b", "c"]) == [(0, 0), (None, 1), (1, 2)]
    assert align(["a", "b"], ["a", "x"]) == [(0, 0), (1, 1)]
    assert align([], ["a"]) == [(None, 0)] and align(["a"], []) == [(0, None)]


def _heard(*words):
    """Words spoken back to back, 0.4 s each."""
    return [Heard(w, round(i * 0.4, 1), round(i * 0.4 + 0.4, 1)) for i, w in enumerate(words)]


def test_a_clean_reading_passes_and_times_the_scripts_own_words():
    check = check_clip("One click marks it done.", _heard("one", "click", "marks", "it", "done"), duration=2.1)
    assert check.wer == 0 and check.ok(0.3)
    # Captions show the script's spelling and punctuation, at the times the words were heard.
    assert check.words[0] == {"w": "One", "start": 0.0, "end": 0.4}
    assert check.words[-1] == {"w": "done.", "start": 1.6, "end": 2.0}


def test_a_misheard_name_counts_against_the_clip_but_keeps_its_timing():
    check = check_clip("Open Acme Corp today", _heard("open", "acne", "corp", "today"), duration=1.6)
    assert check.wer == 0.25 and check.ok(0.3) and not check.ok(0.2)
    assert check.words[1] == {"w": "Acme", "start": 0.4, "end": 0.8}


def test_a_clip_cut_off_at_the_end_fails_however_low_its_error_rate():
    script = "This is a fairly long passage that ends with the word done"
    heard = _heard(*script.lower().split()[:-1])
    check = check_clip(script, heard, duration=5.0)
    assert check.wer == pytest.approx(1 / 12) and check.missing_end and not check.ok(0.3)
    # The unheard word still gets a time: the stretch between the last heard word and the clip's end.
    assert check.words[-1] == {"w": "done", "start": 4.4, "end": 5.0}
    assert check_clip("Hello there world", _heard("there", "world"), 1.0).missing_start


def test_unheard_words_in_the_middle_share_the_gap_they_fall_in():
    heard = [Heard("one", 0.0, 0.4), Heard("four", 1.6, 2.0)]
    words = check_clip("one two three four", heard, duration=2.0).words
    assert [(w["w"], w["start"], w["end"]) for w in words] == [("one", 0.0, 0.4), ("two", 0.4, 1.0), ("three", 1.0, 1.6), ("four", 1.6, 2.0)]


def test_unspoken_marks_are_not_counted_as_missing_words():
    check = check_clip("Fast — and simple.", _heard("fast", "and", "simple"), duration=1.3)
    assert check.wer == 0 and [w["w"] for w in check.words] == ["Fast", "—", "and", "simple."]


def test_silence_or_gibberish_fails():
    assert check_clip("Say this", [], 1.0).wer == 1.0 and not check_clip("Say this", [], 1.0).ok(0.3)
    assert check_clip("Say this", _heard("completely", "different", "words", "here"), 1.6).wer == 2.0


class ScriptedVoice:
    """A text-to-speech and speech-to-text pair that follow a script, clip by clip."""

    def __init__(self, clips, hearings):
        self.clips, self.hearings = list(clips), list(hearings)
        self.spoken = []

    def synthesize(self, text, *, voice, speed):
        self.spoken.append((text, voice, speed))
        return float_to_wav(self.clips.pop(0), RATE)

    def transcribe(self, wav):
        return self.hearings.pop(0)


@pytest.fixture
def voicing(tmp_path, monkeypatch):
    """Run the voicing stage on one passage with scripted speech engines."""
    monkeypatch.setattr(providers, "_instances", {})

    def run(text, clips, hearings, **voice):
        engine = ScriptedVoice(clips, hearings)
        register_plugin("tts", "scripted", lambda **params: engine)
        register_plugin("stt", "scripted", lambda **params: engine)
        config = Config(voice={"tts": {"provider": "scripted", "voice": "v1", "speed": 1.1}, "stt": {"provider": "scripted", "model": "m"}, **voice})
        ctx = StageContext("r1", LocalArtifactStore(tmp_path), config)
        out = REGISTRY["synthesize_step"].fn(ctx, {"step_id": "s1", "text": text})
        return out, ctx.store.get_json(out["check"]), engine, ctx.store

    return run


def test_voicing_saves_a_trimmed_clip_and_its_check(voicing):
    out, report, engine, store = voicing("Ship it now", [_tone(1.0, lead=0.3, tail=0.4)], [_heard("ship", "it", "now")])
    assert engine.spoken == [("Ship it now", "v1", 1.1)]
    samples, rate = read_wav(store.get_bytes(out["audio"]["path"]))
    assert out["audio"]["path"] == "audio/s1.wav" and rate == RATE
    assert out["duration"] == pytest.approx(len(samples) / rate, abs=0.001)
    assert out["duration"] == pytest.approx(1.16, abs=0.01)  # the speech plus its pads; 0.54 s of silence gone
    assert report["ok"] and report["attempts"] == 1 and report["wer"] == 0
    assert [w["w"] for w in report["words"]] == ["Ship", "it", "now"]


def test_a_bad_reading_is_voiced_again(voicing):
    clips = [_tone(0.6), _tone(1.0, amplitude=0.4)]
    out, report, engine, _ = voicing("Ship it now", clips, [_heard("ship"), _heard("ship", "it", "now")])
    assert len(engine.spoken) == 2 and report["ok"] and report["attempts"] == 2
    assert out["duration"] == pytest.approx(1.0, abs=0.01)  # the second clip, not the cut-off first


def test_when_no_reading_passes_the_closest_is_kept_and_flagged(voicing):
    clips = [_tone(1.0), _tone(0.8, amplitude=0.4), _tone(0.5, amplitude=0.3)]
    hearings = [_heard("ship", "it", "cow"), _heard("ship", "at", "cow"), _heard("ship", "it", "now")]
    out, report, engine, _ = voicing("Ship it now", clips, hearings, max_wer=0.2, max_attempts=2)
    assert len(engine.spoken) == 2 and report["attempts"] == 2  # the third clip is never asked for
    assert not report["ok"] and report["wer"] == pytest.approx(0.333) and report["heard"] == "ship it cow"
    assert out["duration"] == pytest.approx(1.0, abs=0.01)  # the first clip: one word wrong beats two


def test_a_voice_that_repeats_itself_exactly_is_not_asked_a_third_time(voicing):
    same = _tone(1.0)
    _, report, engine, _ = voicing("Ship it now", [same, same, same], [_heard("ship")], max_attempts=5)
    assert len(engine.spoken) == 2 and report["attempts"] == 1 and not report["ok"]

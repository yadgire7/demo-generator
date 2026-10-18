"""The real speech engines, Kokoro and Whisper, on real passages. Slow the first time: models download."""

from __future__ import annotations

import pytest

from demogen.voice.audio import read_wav

pytest.importorskip("kokoro")
pytest.importorskip("faster_whisper")

PASSAGES = {"add-first": "Start by typing what needs doing, then press Enter.", "add-second": "Add as many as you like."}


def _story(ctx, inputs):
    steps = [{"step_id": step_id, "text": text} for step_id, text in PASSAGES.items()]
    return {"story": ctx.store.put_json("story.json", {"steps": steps})}


def test_passages_are_voiced_checked_and_timed(harness, playbook, tmp_path):
    data = playbook.read_text().split("  - id: complete-first")[0]  # the example's first two steps
    two_steps = tmp_path / "two.yaml"
    two_steps.write_text(data)
    h = harness({"write_story": _story}, real="voice")
    status = h.start(two_steps, auto_approve=True)
    assert status.phase == "done"

    words = h.json("r1", "words.json")
    assert words["warnings"] == []
    for step_id, text in PASSAGES.items():
        clip = status.state["clips"][step_id]
        samples, rate = read_wav(h.path("r1", clip["audio"]["path"]).read_bytes())
        assert rate == 24000 and clip["duration"] == pytest.approx(len(samples) / rate, abs=0.001)
        # Read at a natural pace: between two and five words a second, with no long silences left on.
        assert 2.0 < len(text.split()) / clip["duration"] < 5.0

        report = h.json("r1", clip["check"]["path"])
        assert report["ok"] and report["wer"] == 0 and report["attempts"] == 1
        timed = words["steps"][step_id]
        assert [w["w"] for w in timed] == text.split()  # the script's own words, for captions
        starts = [w["start"] for w in timed]
        assert starts == sorted(starts) and timed[0]["start"] < 0.3 and timed[-1]["end"] <= clip["duration"] + 0.05
    assert h.json("r1", "report.json")["audio_warnings"] == []
    assert h.json("r1", "edl.json")["steps"][0]["audio_duration"] == status.state["clips"]["add-first"]["duration"]


def test_a_pronunciation_data_path_espeak_cannot_take_is_refused_with_a_reason(monkeypatch):
    import espeakng_loader

    from demogen.errors import DemogenError
    from demogen.voice.tts.kokoro import KokoroTTS

    monkeypatch.setattr(espeakng_loader, "get_data_path", lambda: "/very/deep/" + "folder/" * 25 + "espeak-ng-data")
    with pytest.raises(DemogenError, match="shorter path"):
        KokoroTTS().synthesize("Hello.", voice="af_heart", speed=1.0)

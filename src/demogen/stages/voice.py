"""The voice stages: speak each passage, and check each clip says what it was meant to."""

from __future__ import annotations

from typing import Any

from demogen.stages.base import StageContext, stage
from demogen.voice.audio import read_wav, trim_silence, write_wav
from demogen.voice.check import check_clip
from demogen.voice.providers import stt_for, tts_for


@stage("synthesize_step", pool="gpu", config_keys=("voice",))
def synthesize_step(ctx: StageContext, inputs: dict[str, Any]) -> dict[str, Any]:
    """Voice one passage, hear it back, and keep the take that reads the script best."""
    voice = ctx.config.voice
    tts, stt = tts_for(voice.tts), stt_for(voice.stt)
    step_id, text = inputs["step_id"], inputs["text"]

    best = None
    previous = None
    attempts = 0
    while attempts < voice.max_attempts:
        raw = tts.synthesize(text, voice=voice.tts.voice, speed=voice.tts.speed)
        if raw == previous:
            break  # this voice says it the same way every time; another try changes nothing
        previous = raw
        attempts += 1
        samples, rate = read_wav(raw)
        samples = trim_silence(samples, rate)
        clip, duration = write_wav(samples, rate), len(samples) / rate
        check = check_clip(text, stt.transcribe(clip), duration)
        if best is None or check.wer < best[2].wer:
            best = (clip, duration, check)
        if check.ok(voice.max_wer):
            break

    clip, duration, check = best
    report = {
        "text": text,
        "heard": check.heard,
        "wer": round(check.wer, 3),
        "ok": check.ok(voice.max_wer),
        "missing_start": check.missing_start,
        "missing_end": check.missing_end,
        "attempts": attempts,
        "words": check.words,
    }
    return {
        "audio": ctx.store.put_bytes(f"audio/{step_id}.wav", clip),
        "duration": round(duration, 3),
        "check": ctx.store.put_json(f"audio/{step_id}.json", report),
    }


@stage("qa_audio")
def qa_audio(ctx: StageContext, inputs: dict[str, Any]) -> dict[str, Any]:
    """Gather the clips' word timings for captions, and list the clips a person should listen to."""
    story = ctx.store.get_json(inputs["story"])
    words: dict[str, Any] = {"steps": {}, "warnings": []}
    for step in story["steps"]:
        report = ctx.store.get_json(inputs["clips"][step["step_id"]]["check"])
        words["steps"][step["step_id"]] = report["words"]
        if not report["ok"]:
            words["warnings"].append({"step_id": step["step_id"], "wer": report["wer"], "script": report["text"], "heard": report["heard"]})
    return {"words": ctx.store.put_json("words.json", words)}

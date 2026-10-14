"""What tracing records: runs, stages, model and speech calls, and a trace's path across the job queue."""

from __future__ import annotations

import pytest
from opentelemetry.trace import SpanKind, StatusCode

from demogen import telemetry
from demogen.config import Config
from demogen.errors import DemogenError
from demogen.graph.runtime import RunError
from demogen.llm.base import llm_for
from demogen.stages import StageError
from tests.conftest import SCRIPTED, fake_qa_audio

STAGES = ["load_validate", "setup", "rehearse", "record_take", "verify_take", "extract_keyframes", "analyze_frames", "write_story"]


def _named(spans, name):
    return [s for s in spans() if s.name == name]


def test_each_advance_of_a_run_is_one_trace_with_a_span_per_stage(harness, playbook, spans):
    h = harness()
    h.start(playbook)  # runs up to the plan approval
    (advance,) = _named(spans, "run.advance")
    assert dict(advance.attributes) == {"demogen.run_id": "r1", "demogen.step": "start", "demogen.phase": "awaiting_approval"}
    assert advance.parent is None

    stages = [s for s in spans() if s.name.startswith("stage ")]
    assert [s.name for s in stages] == ["stage load_validate", "stage setup", "stage rehearse"]
    for stage in stages:
        assert stage.context.trace_id == advance.context.trace_id and stage.parent.span_id == advance.context.span_id
        assert stage.attributes["demogen.run_id"] == "r1" and stage.attributes["demogen.cache_hit"] is False
    assert {s.attributes["demogen.stage"]: s.attributes["demogen.pool"] for s in stages} == {"load_validate": "cpu", "setup": "browser", "rehearse": "browser"}

    h.runtime.approve("r1")
    advances = _named(spans, "run.advance")
    assert [a.attributes["demogen.step"] for a in advances] == ["start", "approve"]
    assert advances[0].context.trace_id != advances[1].context.trace_id  # a new trace each time the run moves on


def test_fanned_out_stages_stay_in_the_runs_trace(harness, playbook, spans):
    h = harness()
    h.start(playbook, auto_approve=True)
    (advance,) = _named(spans, "run.advance")
    voiced = _named(spans, "stage synthesize_step")
    assert len(voiced) == 4  # these run on other threads, in parallel
    assert all(s.context.trace_id == advance.context.trace_id and s.parent.span_id == advance.context.span_id for s in voiced)
    assert advance.attributes["demogen.phase"] == "done"


def test_cached_and_failed_stages_are_marked(harness, playbook, spans):
    failures = iter([StageError("the speech checker ran out of memory")])

    def flaky(ctx, inputs):
        for failure in failures:
            raise failure
        return fake_qa_audio(ctx, inputs)

    h = harness({"qa_audio": flaky})
    with pytest.raises(RunError):
        h.start(playbook, auto_approve=True)
    (failed,) = _named(spans, "stage qa_audio")
    assert failed.status.status_code == StatusCode.ERROR and "ran out of memory" in failed.status.description
    assert failed.events[0].name == "exception"
    (advance,) = _named(spans, "run.advance")
    assert advance.status.status_code == StatusCode.ERROR

    h.runtime.rerun("r1", "verify_take")  # replays from a stage whose later results are all cached
    rerun = _named(spans, "run.advance")[-1]
    assert (rerun.attributes["demogen.step"], rerun.attributes["demogen.from_stage"]) == ("rerun", "verify_take")
    again = {s.attributes["demogen.stage"]: s.attributes["demogen.cache_hit"] for s in spans() if s.name.startswith("stage ") and s.context.trace_id == rerun.context.trace_id}
    assert again["verify_take"] is False and again["extract_keyframes"] is True and again["qa_audio"] is False


def test_model_calls_record_the_model_and_tokens_but_never_the_words(scripted_llm, spans):
    scripted_llm.replies = [{"steps": [{"step_id": "a", "text": "A secret-looking reply."}]}]
    from demogen.story.schema import Story

    llm = llm_for(Config(**SCRIPTED), "writer")
    llm.complete([{"role": "user", "content": "A prompt with customer data in it."}], response_schema=Story)
    (call,) = _named(spans, "chat script")
    assert call.kind == SpanKind.CLIENT
    assert dict(call.attributes) == {
        "gen_ai.operation.name": "chat", "gen_ai.provider.name": "scripted", "gen_ai.request.model": "script",
        "demogen.llm.role": "writer", "gen_ai.usage.input_tokens": 0, "gen_ai.usage.output_tokens": 0, "demogen.llm.tool_calls": 0,
    }  # fmt: skip
    assert "customer data" not in str(call.to_json()) and "secret-looking" not in str(call.to_json())


def test_speech_calls_are_spans_inside_their_stage(spans, tmp_path, monkeypatch):
    import numpy as np

    from demogen.plugins import register_plugin
    from demogen.stages import REGISTRY, StageContext
    from demogen.store.artifacts import LocalArtifactStore
    from demogen.voice import providers
    from demogen.voice.audio import float_to_wav
    from demogen.voice.check import Heard

    class Engine:
        def synthesize(self, text, *, voice, speed):
            return float_to_wav((0.4 * np.sin(np.arange(24000) / 20)).astype(np.float32), 24000)

        def transcribe(self, wav):
            return [Heard("ship", 0.0, 0.5), Heard("it", 0.5, 1.0)]

    monkeypatch.setattr(providers, "_instances", {})
    register_plugin("tts", "scripted", lambda **params: Engine())
    register_plugin("stt", "scripted", lambda **params: Engine())
    config = Config(voice={"tts": {"provider": "scripted", "voice": "v1"}, "stt": {"provider": "scripted", "model": "m"}})
    REGISTRY["synthesize_step"].fn(StageContext("r1", LocalArtifactStore(tmp_path), config), {"step_id": "s1", "text": "Ship it"})
    (spoken,), (heard,) = _named(spans, "tts.synthesize"), _named(spans, "stt.transcribe")
    assert dict(spoken.attributes) == {"demogen.tts.provider": "scripted", "demogen.tts.voice": "v1", "demogen.tts.characters": 7}
    assert dict(heard.attributes) == {"demogen.stt.provider": "scripted", "demogen.stt.model": "m", "demogen.stt.words": 2}


def test_a_trace_can_be_carried_to_another_place_and_continued(spans):
    with telemetry.span("queued here") as origin:
        headers = telemetry.carrier()
    assert set(headers) == {"traceparent"}
    with telemetry.continued(headers):  # in a worker, say
        with telemetry.span("ran there"):
            pass
    with telemetry.continued(None):  # a job queued before tracing was on: a fresh trace, no error
        with telemetry.span("no parent"):
            pass
    (there,), (orphan,) = _named(spans, "ran there"), _named(spans, "no parent")
    assert there.context.trace_id == origin.get_span_context().trace_id and there.parent.span_id == origin.get_span_context().span_id
    assert orphan.parent is None


@pytest.mark.parametrize(
    "env,processor",
    [
        ({}, None),
        ({"OTEL_TRACES_EXPORTER": "none", "OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector:4318"}, None),
        ({"OTEL_SDK_DISABLED": "true", "OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector:4318"}, None),
        ({"OTEL_TRACES_EXPORTER": "console"}, "ConsoleSpanExporter"),
        ({"OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector:4318"}, "OTLPSpanExporter"),
        ({"OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": "http://collector:4318/v1/traces"}, "OTLPSpanExporter"),
    ],
)
def test_tracing_turns_on_only_when_the_environment_asks(env, processor):
    provider = telemetry.build_provider("worker", env)
    if processor is None:
        assert provider is None
        return
    try:
        (batch,) = provider._active_span_processor._span_processors
        assert type(batch.span_exporter).__name__ == processor
        assert provider.resource.attributes["service.name"] == "demogen-worker"
    finally:
        provider.shutdown()


def test_an_unknown_exporter_is_refused():
    with pytest.raises(DemogenError, match="use otlp, console or none"):
        telemetry.build_provider("cli", {"OTEL_TRACES_EXPORTER": "zipkin"})


def test_cached_tokens_are_reported_apart_from_the_total(spans):
    from demogen.llm.base import LLMResult, TracedLLM, Usage, assistant_message

    class Cached:
        def complete(self, messages, *, tools=None, response_schema=None):
            return LLMResult(text="ok", usage=Usage(input_tokens=2525, output_tokens=47, cache_read_tokens=2000, cache_write_tokens=521), message=assistant_message("ok", []))

    TracedLLM(Cached(), "anthropic", "m", "planner").complete([])
    attributes = _named(spans, "chat m")[0].attributes
    assert attributes["gen_ai.usage.input_tokens"] == 2525
    assert (attributes["gen_ai.usage.cache_read.input_tokens"], attributes["gen_ai.usage.cache_creation.input_tokens"]) == (2000, 521)

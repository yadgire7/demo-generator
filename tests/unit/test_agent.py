"""The tool loop, and how the Anthropic adapter translates the neutral message shape."""

from types import SimpleNamespace

import pytest

from demogen.llm import anthropic_provider
from demogen.llm.agent_loop import NUDGE, Stop, ToolError, run_tool_loop
from demogen.llm.anthropic_provider import FALLBACK_BETA, AnthropicProvider
from demogen.llm.base import IS_ERROR, NATIVE, LLMError, LLMResult, ToolCall, assistant_message, tool


class Script:
    def __init__(self, *turns):
        self.turns = list(turns)

    def complete(self, messages, *, tools=None, response_schema=None):
        calls = [ToolCall(id=f"c{i}", name=name, arguments=args) for i, (name, args) in enumerate(self.turns.pop(0))]
        return LLMResult(text=None if calls else "thinking aloud", tool_calls=calls, message=assistant_message(None, calls))


def test_loop_runs_tools_until_one_stops_it():
    log = []
    handlers = {"note": lambda text: log.append(text) or "noted", "done": lambda: Stop("finished")}
    llm = Script([("note", {"text": "a"}), ("note", {"text": "b"})], [("done", {})])
    messages = [{"role": "user", "content": "go"}]
    assert run_tool_loop(llm, messages, [], handlers, max_turns=5, observe=lambda: "world state") == "finished"
    assert log == ["a", "b"]
    results = [m for m in messages if m["role"] == "tool"]
    # One result per call; the observation rides on the last result of a turn that did not stop.
    assert [r["content"] for r in results] == ["noted", "noted\n\nworld state", "ok"]


def test_loop_reports_tool_failures_to_the_model_and_carries_on():
    def flaky(ref):
        raise ToolError(f"ref {ref} is stale")

    llm = Script([("click", {"ref": "e1"}), ("nope", {}), ("click", {})], [("done", {})])
    messages = []
    run_tool_loop(llm, messages, [], {"click": flaky, "done": lambda: Stop(None)}, max_turns=3)
    errors = [m for m in messages if m.get(IS_ERROR)]
    assert [e["content"].split(":")[1].strip() for e in errors] == ["ref e1 is stale", "there is no tool named nope", "bad arguments for click"]


def test_loop_nudges_a_model_that_only_talks_then_gives_up():
    messages = []
    with pytest.raises(LLMError, match="within 2 turns"):
        run_tool_loop(Script([], []), messages, [], {}, max_turns=2)
    assert [m["content"] for m in messages if m["role"] == "user"] == [NUDGE, NUDGE]


def _block(kind, **fields):
    return SimpleNamespace(type=kind, **fields)


def _response(*content, stop_reason="end_turn", stop_details=None):
    return SimpleNamespace(
        content=list(content), stop_reason=stop_reason, stop_details=stop_details,
        usage=SimpleNamespace(input_tokens=11, output_tokens=7),
    )  # fmt: skip


@pytest.fixture
def claude(monkeypatch):
    """An AnthropicProvider whose client records requests and returns queued responses."""
    sent, queue = [], []

    class FakeMessages:
        def create(self, **request):
            sent.append(request)
            return queue.pop(0)

    monkeypatch.setattr(anthropic_provider.anthropic, "Anthropic", lambda: SimpleNamespace(beta=SimpleNamespace(messages=FakeMessages())))
    return AnthropicProvider("claude-opus-5-5"), sent, queue


def test_anthropic_request_shape(claude):
    llm, sent, queue = claude
    queue.append(_response(_block("thinking", thinking=""), _block("tool_use", id="tu1", name="click", input={"ref": "e5"})))
    tools = [tool("click", "Click an element.", {"ref": {"type": "string"}})]
    result = llm.complete([{"role": "system", "content": "Be direct."}, {"role": "user", "content": "go"}], tools=tools)

    request = sent[0]
    assert request["model"] == "claude-opus-5-5" and request["max_tokens"] == 16000
    assert request["system"] == [{"type": "text", "text": "Be direct."}]
    assert request["messages"] == [{"role": "user", "content": "go"}]
    assert request["tools"] == [{
        "name": "click", "description": "Click an element.", "strict": True,
        "input_schema": {"type": "object", "properties": {"ref": {"type": "string"}}, "required": ["ref"], "additionalProperties": False},
    }]  # fmt: skip
    assert request["output_config"] == {"effort": "medium"}
    assert request["betas"] == [FALLBACK_BETA] and request["fallbacks"] == "default"
    assert "thinking" not in request and "tool_choice" not in request and "temperature" not in request
    assert request["cache_control"] == {"type": "ephemeral"}  # a tool loop resends its conversation each turn

    assert [(c.id, c.name, c.arguments) for c in result.tool_calls] == [("tu1", "click", {"ref": "e5"})]
    assert (result.usage.input_tokens, result.usage.output_tokens) == (11, 7)


def test_anthropic_replays_its_own_turns_untouched_and_groups_tool_results(claude):
    llm, sent, queue = claude
    first = _response(
        _block("thinking", thinking=""),
        _block("tool_use", id="a", name="fill", input={"ref": "e1", "text": "x"}),
        _block("tool_use", id="b", name="press", input={"key": "Enter"}),
    )
    queue.extend([first, _response(_block("text", text="done"))])
    messages = [{"role": "user", "content": "go"}]
    messages.append(llm.complete(messages).message)
    messages.append({"role": "tool", "tool_call_id": "a", "content": "ok"})
    messages.append({"role": "tool", "tool_call_id": "b", "content": "Error: no field is focused", IS_ERROR: True})
    assert llm.complete(messages).text == "done"

    replayed = sent[1]["messages"]
    assert replayed[1] == {"role": "assistant", "content": first.content}  # thinking block included, byte for byte
    assert replayed[1]["content"] is messages[1][NATIVE]
    assert replayed[2] == {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "a", "content": "ok"},
        {"type": "tool_result", "tool_use_id": "b", "content": "Error: no field is focused", "is_error": True},
    ]}  # fmt: skip


def test_anthropic_translates_turns_from_another_provider_and_images(claude):
    llm, sent, queue = claude
    queue.append(_response(_block("text", text="a cat")))
    foreign = assistant_message("Looking.", [ToolCall(id="x", name="snapshot", arguments={})])
    image = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}
    llm.complete([
        {"role": "user", "content": [image, {"type": "text", "text": "What is this?"}]},
        foreign,
        {"role": "tool", "tool_call_id": "x", "content": "ok"},
    ])  # fmt: skip
    user, assistant, _ = sent[0]["messages"]
    assert user["content"][0] == {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}}
    assert assistant["content"] == [{"type": "text", "text": "Looking."}, {"type": "tool_use", "id": "x", "name": "snapshot", "input": {}}]


def test_anthropic_refusal_and_truncation_are_errors(claude):
    llm, _, queue = claude
    queue.append(_response(stop_reason="refusal", stop_details=SimpleNamespace(category="cyber")))
    with pytest.raises(LLMError, match="declined.*cyber"):
        llm.complete([{"role": "user", "content": "?"}])
    queue.append(_response(_block("text", text="partial"), stop_reason="max_tokens"))
    with pytest.raises(LLMError, match="cut off"):
        llm.complete([{"role": "user", "content": "?"}])


def test_anthropic_counts_cached_input_and_caches_only_tool_loops(claude):
    llm, sent, queue = claude
    cached = _response(_block("text", text="ok"))
    cached.usage = SimpleNamespace(input_tokens=4, output_tokens=47, cache_read_input_tokens=2000, cache_creation_input_tokens=521)
    queue.append(cached)
    usage = llm.complete([{"role": "user", "content": "one question, asked once"}]).usage
    # The API's `input_tokens` leaves out what was cached; the total is what the model actually read.
    assert (usage.input_tokens, usage.cache_read_tokens, usage.cache_write_tokens, usage.output_tokens) == (2525, 2000, 521, 47)
    assert "cache_control" not in sent[0]  # nothing would ever read a one-off request's cache entry

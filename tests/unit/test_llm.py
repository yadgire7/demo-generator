from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from demogen.config import Config, ModelConfig
from demogen.llm import litellm_provider
from demogen.llm.base import LLMError, llm_for
from demogen.llm.litellm_provider import LiteLLMProvider


def _response(content=None, tool_calls=None, usage=(10, 5)):
    message = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message)],
        usage=SimpleNamespace(prompt_tokens=usage[0], completion_tokens=usage[1]),
    )


@pytest.fixture
def completions(monkeypatch):
    """Queue canned provider responses and record what was sent."""
    queue, sent = [], []

    def fake_completion(**kwargs):
        sent.append(kwargs)
        return queue.pop(0)

    monkeypatch.setattr(litellm_provider.litellm, "completion", fake_completion)
    return queue, sent


class Verdict(BaseModel):
    ok: bool


def test_text_and_tool_calls_are_normalised(completions):
    queue, sent = completions
    call = SimpleNamespace(id="c1", function=SimpleNamespace(name="click", arguments='{"ref": "e5"}'))
    queue.append(_response(content="clicking", tool_calls=[call]))
    tools = [{"type": "function", "function": {"name": "click", "parameters": {}}}]
    result = LiteLLMProvider("p/m", temperature=0).complete([{"role": "user", "content": "go"}], tools=tools)
    assert result.text == "clicking"
    assert [(c.id, c.name, c.arguments) for c in result.tool_calls] == [("c1", "click", {"ref": "e5"})]
    assert (result.usage.input_tokens, result.usage.output_tokens) == (10, 5)
    assert sent[0]["model"] == "p/m" and sent[0]["temperature"] == 0 and sent[0]["tools"] == tools


def test_structured_output_is_parsed(completions):
    queue, sent = completions
    queue.append(_response(content='{"ok": true}'))
    result = LiteLLMProvider("p/m").complete([{"role": "user", "content": "?"}], response_schema=Verdict)
    assert result.parsed == Verdict(ok=True)
    assert sent[0]["response_format"] is Verdict


def test_structured_output_gets_one_corrective_retry(completions):
    queue, sent = completions
    queue += [_response(content="sure!"), _response(content='{"ok": false}')]
    result = LiteLLMProvider("p/m").complete([{"role": "user", "content": "?"}], response_schema=Verdict)
    assert result.parsed == Verdict(ok=False)
    assert len(sent) == 2 and sent[1]["messages"][-2] == {"role": "assistant", "content": "sure!"}
    assert result.usage.input_tokens == 20


def test_llm_for_resolves_the_configured_role():
    config = Config(models={"writer": ModelConfig(model="p/m", params={"temperature": 0.2})})
    provider = llm_for(config, "writer")
    adapter = provider.inner  # the adapter itself sits inside a wrapper that traces its calls
    assert isinstance(adapter, LiteLLMProvider) and adapter.model == "p/m" and adapter.params == {"temperature": 0.2}
    with pytest.raises(LLMError, match="vision"):
        llm_for(config, "vision")

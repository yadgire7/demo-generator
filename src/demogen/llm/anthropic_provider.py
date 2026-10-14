from __future__ import annotations

import json
from typing import Any

import anthropic
from pydantic import BaseModel

from demogen.llm.base import IS_ERROR, NATIVE, LLMError, LLMResult, Message, ToolCall, Usage, assistant_message

# A declined request is re-run server-side on a model chosen for the refusal category.
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AnthropicProvider:
    """Claude through the official SDK.

    History is replayed append-only: each assistant turn goes back exactly as it was received
    (its native blocks ride along on the message), because the model's thinking blocks are
    only valid in an unedited conversation.
    """

    def __init__(self, model: str, *, effort: str = "medium", max_tokens: int = 16000, **params: Any):
        self.model = model
        self.effort = effort
        self.max_tokens = max_tokens
        self.params = params
        self.client = anthropic.Anthropic()

    def complete(
        self,
        messages: list[Message],
        *,
        tools: list[dict[str, Any]] | None = None,
        response_schema: type[BaseModel] | None = None,
    ) -> LLMResult:
        system, turns = _to_anthropic(messages)
        request: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": turns,
            "output_config": {"effort": self.effort},
            "betas": [FALLBACK_BETA],
            "fallbacks": "default",
            **self.params,
        }
        if system:
            request["system"] = system
        if tools:
            request["tools"] = [_to_anthropic_tool(tool) for tool in tools]
            # A tool loop sends the same growing conversation every turn, so caching it pays for
            # itself from the second turn. A one-off request would only pay the cache-write premium.
            request["cache_control"] = {"type": "ephemeral"}
        if response_schema is not None:
            request["output_config"]["format"] = {"type": "json_schema", "schema": _strict(response_schema.model_json_schema())}

        try:
            response = self.client.beta.messages.create(**request)
        except anthropic.AuthenticationError as error:
            raise LLMError("Anthropic rejected the API key (check ANTHROPIC_API_KEY)") from error
        except anthropic.RateLimitError as error:
            raise LLMError("Anthropic rate limit reached; wait a little and resume the run") from error
        except anthropic.APIStatusError as error:
            raise LLMError(f"Anthropic API error {error.status_code}: {error.message}") from error
        except anthropic.APIConnectionError as error:
            raise LLMError("could not reach the Anthropic API (network problem)") from error

        if response.stop_reason == "refusal":
            category = response.stop_details.category if response.stop_details else None
            raise LLMError(f"the model declined this request (category: {category or 'unspecified'})")
        if response.stop_reason == "max_tokens":
            raise LLMError(f"the model's reply was cut off at {self.max_tokens} tokens")

        text = "".join(block.text for block in response.content if block.type == "text") or None
        tool_calls = [
            ToolCall(id=block.id, name=block.name, arguments=dict(block.input))
            for block in response.content
            if block.type == "tool_use"
        ]
        return LLMResult(
            text=text,
            tool_calls=tool_calls,
            parsed=response_schema.model_validate_json(text) if response_schema is not None and text else None,
            usage=_usage(response.usage),
            message=assistant_message(text, tool_calls, native=response.content),
        )


def _usage(usage: Any) -> Usage:
    # The API reports cached input apart from `input_tokens`, which counts only what was not cached.
    read = getattr(usage, "cache_read_input_tokens", None) or 0
    written = getattr(usage, "cache_creation_input_tokens", None) or 0
    return Usage(
        input_tokens=usage.input_tokens + read + written,
        output_tokens=usage.output_tokens,
        cache_read_tokens=read,
        cache_write_tokens=written,
    )


def _to_anthropic(messages: list[Message]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    system: list[dict[str, Any]] = []
    turns: list[dict[str, Any]] = []
    for message in messages:
        role = message["role"]
        if role == "system":
            system.append({"type": "text", "text": message["content"]})
        elif role == "tool":
            block = {"type": "tool_result", "tool_use_id": message["tool_call_id"], "content": message["content"]}
            if message.get(IS_ERROR):
                block["is_error"] = True
            # All results of one assistant turn go back together, in a single user turn.
            if turns and turns[-1]["role"] == "user" and _is_tool_results(turns[-1]["content"]):
                turns[-1]["content"].append(block)
            else:
                turns.append({"role": "user", "content": [block]})
        elif role == "assistant":
            turns.append({"role": "assistant", "content": message.get(NATIVE) or _assistant_blocks(message)})
        else:
            turns.append({"role": "user", "content": _user_content(message["content"])})
    return system, turns


def _is_tool_results(content: Any) -> bool:
    return isinstance(content, list) and all(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)


def _assistant_blocks(message: Message) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    if message.get("content"):
        blocks.append({"type": "text", "text": message["content"]})
    for call in message.get("tool_calls") or []:
        function = call["function"]
        blocks.append({"type": "tool_use", "id": call["id"], "name": function["name"], "input": json.loads(function["arguments"])})
    return blocks


def _user_content(content: Any) -> Any:
    if isinstance(content, str):
        return content
    blocks = []
    for part in content:
        if part["type"] == "image_url":
            header, data = part["image_url"]["url"].split(",", 1)  # data:<mime>;base64,<data>
            media_type = header.removeprefix("data:").removesuffix(";base64")
            blocks.append({"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}})
        else:
            blocks.append(part)
    return blocks


def _to_anthropic_tool(tool: dict[str, Any]) -> dict[str, Any]:
    function = tool["function"]
    converted = {"name": function["name"], "description": function.get("description", ""), "input_schema": function["parameters"]}
    if function.get("strict"):
        converted["strict"] = True
    return converted


def _strict(schema: Any) -> Any:
    """Structured output needs every object closed to extra keys."""
    if isinstance(schema, dict):
        if schema.get("type") == "object":
            schema.setdefault("additionalProperties", False)
        for value in schema.values():
            _strict(value)
    elif isinstance(schema, list):
        for value in schema:
            _strict(value)
    return schema

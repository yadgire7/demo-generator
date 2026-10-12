from __future__ import annotations

import json
from typing import Any

import litellm
from pydantic import BaseModel, ValidationError

from demogen.llm.base import IS_ERROR, NATIVE, LLMResult, Message, ToolCall, Usage, assistant_message


class LiteLLMProvider:
    """Default adapter: one call shape across Anthropic, OpenAI, Gemini, Bedrock, Ollama, vLLM and more."""

    def __init__(self, model: str, **params: Any):
        self.model = model
        self.params = params

    def complete(
        self,
        messages: list[Message],
        *,
        tools: list[dict[str, Any]] | None = None,
        response_schema: type[BaseModel] | None = None,
    ) -> LLMResult:
        result = self._call(messages, tools, response_schema)
        if response_schema is None or result.text is None:
            return result
        try:
            result.parsed = response_schema.model_validate_json(result.text)
        except ValidationError as error:
            # Not every provider enforces the schema; give the model one chance to correct itself.
            retry = self._call(
                [
                    *messages,
                    {"role": "assistant", "content": result.text},
                    {"role": "user", "content": f"That did not match the schema:\n{error}\nReply with corrected JSON only."},
                ],
                tools,
                response_schema,
            )
            retry.parsed = response_schema.model_validate_json(retry.text or "")
            retry.usage.input_tokens += result.usage.input_tokens
            retry.usage.output_tokens += result.usage.output_tokens
            return retry
        return result

    def _call(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None,
        response_schema: type[BaseModel] | None,
    ) -> LLMResult:
        kwargs: dict[str, Any] = dict(self.params)
        if tools:
            kwargs["tools"] = tools
        if response_schema is not None:
            kwargs["response_format"] = response_schema
        portable = [{k: v for k, v in m.items() if k not in (NATIVE, IS_ERROR)} for m in messages]
        response = litellm.completion(model=self.model, messages=portable, **kwargs)
        message = response.choices[0].message
        usage = getattr(response, "usage", None)
        tool_calls = [
            ToolCall(id=call.id, name=call.function.name, arguments=json.loads(call.function.arguments or "{}"))
            for call in (message.tool_calls or [])
        ]
        return LLMResult(
            text=message.content,
            tool_calls=tool_calls,
            message=assistant_message(message.content, tool_calls),
            usage=Usage(
                input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                output_tokens=getattr(usage, "completion_tokens", 0) or 0,
            ),
        )

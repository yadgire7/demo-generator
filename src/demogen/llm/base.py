"""The one LLM interface the rest of the system sees. No vendor SDK is imported outside an adapter.

Messages and tool definitions use the OpenAI chat shape, which every adapter can translate from.
"""

from __future__ import annotations

import base64
import json
import mimetypes
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel

from demogen.config import Config, Role
from demogen.errors import DemogenError
from demogen.plugins import load_plugin

Message = dict[str, Any]

# Message keys that never leave this process: an adapter's own copy of an assistant turn, kept so
# it can replay that turn to its provider exactly as received, and a tool result's error flag.
NATIVE = "_native"
IS_ERROR = "is_error"


class LLMError(DemogenError):
    pass


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class LLMResult:
    text: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    parsed: BaseModel | None = None
    usage: Usage = field(default_factory=Usage)
    # The assistant turn, ready to append to `messages` for the next call.
    message: Message = field(default_factory=dict)


def assistant_message(text: str | None, tool_calls: list[ToolCall], native: Any = None) -> Message:
    message: Message = {"role": "assistant", "content": text}
    if tool_calls:
        message["tool_calls"] = [
            {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": json.dumps(c.arguments)}}
            for c in tool_calls
        ]
    if native is not None:
        message[NATIVE] = native
    return message


def tool(name: str, description: str, properties: dict[str, Any] | None = None) -> dict[str, Any]:
    """A tool definition with every property required and nothing else allowed."""
    properties = properties or {}
    parameters = {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }
    return {"type": "function", "function": {"name": name, "description": description, "parameters": parameters, "strict": True}}


class LLMProvider(Protocol):
    def complete(
        self,
        messages: list[Message],
        *,
        tools: list[dict[str, Any]] | None = None,
        response_schema: type[BaseModel] | None = None,
    ) -> LLMResult: ...


def image_part(path: Path) -> dict[str, Any]:
    """A message content part carrying an image, for vision-capable models."""
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    data = base64.b64encode(path.read_bytes()).decode()
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}}


def llm_for(config: Config, role: Role) -> LLMProvider:
    if role not in config.models:
        raise LLMError(f"no model configured for the {role!r} role (set models.{role} in demogen.yaml)")
    spec = config.models[role]
    return load_plugin("llm", spec.provider)(model=spec.model, **spec.params)

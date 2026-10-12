"""A provider-neutral tool loop: the model calls tools until one of them ends the task."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from demogen.llm.base import IS_ERROR, LLMError, LLMProvider, Message


class ToolError(Exception):
    """Raised by a tool handler to tell the model its call did not work, and why."""


@dataclass
class Stop:
    """Returned by a tool handler to end the loop with a result."""

    value: Any


Handler = Callable[..., "str | Stop"]

NUDGE = "Use the tools to carry out the task, and call the finishing tool when it is done."


def run_tool_loop(
    llm: LLMProvider,
    messages: list[Message],
    tools: list[dict[str, Any]],
    handlers: dict[str, Handler],
    *,
    max_turns: int,
    observe: Callable[[], str] | None = None,
) -> Any:
    """Run until a handler returns `Stop`. `observe` describes the world after each turn's tools ran."""
    for _ in range(max_turns):
        result = llm.complete(messages, tools=tools)
        messages.append(result.message)
        if not result.tool_calls:
            messages.append({"role": "user", "content": NUDGE})
            continue
        stop: Stop | None = None
        replies: list[Message] = []
        for call in result.tool_calls:
            reply: Message = {"role": "tool", "tool_call_id": call.id, "content": "ok"}
            try:
                if call.name not in handlers:
                    raise ToolError(f"there is no tool named {call.name}")
                outcome = handlers[call.name](**call.arguments)
            except ToolError as error:
                reply.update({"content": f"Error: {error}", IS_ERROR: True})
            except TypeError as error:  # wrong or missing arguments
                reply.update({"content": f"Error: bad arguments for {call.name}: {error}", IS_ERROR: True})
            else:
                if isinstance(outcome, Stop):
                    stop = outcome
                else:
                    reply["content"] = outcome
            replies.append(reply)
        if stop is None and observe is not None:
            replies[-1]["content"] += "\n\n" + observe()
        messages.extend(replies)
        if stop is not None:
            return stop.value
    raise LLMError(f"the model did not finish the task within {max_turns} turns")

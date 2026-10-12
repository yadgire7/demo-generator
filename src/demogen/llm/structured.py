"""Ask for a structured reply, check it, and let the model correct what the check finds."""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

from pydantic import BaseModel

from demogen.llm.base import LLMError, LLMProvider, Message

T = TypeVar("T", bound=BaseModel)

# What a check reports: problems that make the reply unusable, and ones that only make it worse.
Problems = tuple[list[str], list[str]]


def complete_checked(
    llm: LLMProvider, messages: list[Message], schema: type[T], check: Callable[[T], Problems], *, attempts: int = 3
) -> T:
    """Return a reply that passes `check`.

    If attempts run out, a reply with only minor problems is still returned; one that is
    unusable raises `LLMError`.
    """
    messages = list(messages)
    for attempt in range(attempts):
        result = llm.complete(messages, response_schema=schema)
        if result.parsed is None:
            raise LLMError("the model did not return the structured reply that was asked for")
        fatal, minor = check(result.parsed)
        if not fatal and not minor:
            return result.parsed
        if attempt + 1 < attempts:
            listed = "\n".join(f"- {problem}" for problem in fatal + minor)
            messages += [result.message, {"role": "user", "content": f"Please fix these and reply again in full:\n{listed}"}]
    if fatal:
        raise LLMError(f"the model's reply stayed unusable after {attempts} attempts: {'; '.join(fatal)}")
    return result.parsed

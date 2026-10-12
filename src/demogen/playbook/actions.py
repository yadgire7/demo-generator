"""The vocabulary of browser actions, shared by playbooks (explicit overrides) and the lockfile."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, model_validator

ActionType = Literal["goto", "click", "fill", "press", "hover", "select"]
_ELEMENT_ACTIONS = {"click", "fill", "hover", "select"}


class Locator(BaseModel):
    """One way to find an element: by accessible role and name, or by CSS selector."""

    model_config = ConfigDict(extra="forbid")

    by: Literal["role", "css"]
    role: str | None = None
    name: str | None = None
    selector: str | None = None
    nth: int | None = None  # index among several matches, when the locator alone is ambiguous

    @model_validator(mode="after")
    def _complete(self) -> Locator:
        if self.by == "role" and not self.role:
            raise ValueError("a role locator needs `role`")
        if self.by == "css" and not self.selector:
            raise ValueError("a css locator needs `selector`")
        return self


class Action(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: ActionType
    target: list[Locator] = []  # candidates, most durable first
    label: str | None = None  # what a person would call the element
    text: str | None = None  # typed text or selected option; may hold ${secret:NAME}
    key: str | None = None
    url: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _from_shorthand(cls, value: Any) -> Any:
        """Accept the playbook form: `{click: {role: button, name: Save}}`, `{press: Enter}`, ..."""
        if not isinstance(value, dict) or "type" in value or len(value) != 1:
            return value
        (kind, arg), = value.items()
        if kind == "press":
            return {"type": "press", "key": arg}
        if kind == "goto":
            return {"type": "goto", "url": arg}
        if kind not in _ELEMENT_ACTIONS or not isinstance(arg, dict):
            return value
        arg = dict(arg)
        text = arg.pop("text", None) if kind == "fill" else arg.pop("value", None) if kind == "select" else None
        locator = {"by": "css", "selector": arg.pop("css")} if "css" in arg else {"by": "role", **arg}
        return {"type": kind, "target": [locator], "text": text}

    @model_validator(mode="after")
    def _complete(self) -> Action:
        if self.type in _ELEMENT_ACTIONS and not self.target:
            raise ValueError(f"`{self.type}` needs an element (role and name, or css)")
        if self.type in ("fill", "select") and self.text is None:
            raise ValueError(f"`{self.type}` needs the {'text' if self.type == 'fill' else 'value'} to enter")
        if self.type == "press" and not self.key:
            raise ValueError("`press` needs a key")
        if self.type == "goto" and not self.url:
            raise ValueError("`goto` needs a url")
        return self

    def describe(self) -> str:
        subject = self.label or (self.target[0].name or self.target[0].selector if self.target else None)
        detail = {"goto": self.url, "press": self.key}.get(self.type, subject)
        return f"{self.type} {detail}"

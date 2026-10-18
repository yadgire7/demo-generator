"""What each review screen needs, shaped for people: steps with their words, actions in plain language."""

from __future__ import annotations

import re
from typing import Any

import yaml

from demogen.graph.runtime import Runtime
from demogen.playbook.secrets import secret_names
from demogen.story.budget import Budget

_LABEL = re.compile(r'^(?P<role>[a-z]+)(?: "(?P<name>.*)")?$')
# What to call an element of each role in a sentence.
_NOUNS = {
    "textbox": "box", "searchbox": "box", "combobox": "box", "spinbutton": "field", "button": "button", "link": "link",
    "checkbox": "checkbox", "radio": "option", "tab": "tab", "menuitem": "menu item", "option": "option", "heading": "heading",
    "img": "picture", "switch": "switch", "slider": "slider", "row": "row", "cell": "cell",
}  # fmt: skip


def element_words(action: dict[str, Any]) -> str:
    """The element an action is aimed at, as a person would name it: 'the "Search" box'."""
    match = _LABEL.match(action.get("label") or "")
    if not match:
        target = (action.get("target") or [{}])[0]
        name = target.get("name")
        role = target.get("role") or "element"
    else:
        name, role = match.group("name"), match.group("role")
    noun = _NOUNS.get(role, role if role not in ("generic", "element") else "element")
    return f'the "{name}" {noun}' if name else f"a {noun} with no visible name"


def typed_words(text: str | None) -> str:
    """Typed text as it can be shown: a credential is named, never shown."""
    names = secret_names(text or "")
    if names:
        return "your " + " and ".join(name.replace("_", " ").lower() for name in sorted(names)) + " (kept secret)"
    return f"“{text}”"


def describe_action(action: dict[str, Any]) -> str:
    kind = action["type"]
    if kind == "fill":
        return f"Type {typed_words(action.get('text'))} into {element_words(action)}"
    if kind == "click":
        return f"Click {element_words(action)}"
    if kind == "hover":
        return f"Point at {element_words(action)}"
    if kind == "select":
        return f"Choose {typed_words(action.get('text'))} in {element_words(action)}"
    if kind == "press":
        return f"Press {action.get('key')}"
    if kind == "goto":
        return f"Go to {action.get('url')}"
    return kind


def review(runtime: Runtime, run_id: str) -> dict[str, Any]:
    """Everything the screen for the run's current pause shows. Raises LookupError if it is not paused."""
    status = runtime.status(run_id)
    if status.phase != "awaiting_approval":
        raise LookupError(f"run {run_id} is not waiting for a review")
    kind = status.awaiting["kind"]
    store = runtime.context(run_id).store
    if kind == "playbook":
        text = store.get_bytes(status.awaiting["artifact"]["path"]).decode()
        return {
            "kind": "playbook",
            "yaml": text,
            "playbook": yaml.safe_load(text),
            "can_feedback": bool(status.state.get("prompt_src")),
            "failure": status.awaiting.get("failure"),
        }

    playbook = store.get_json("playbook.json")
    do = {step["id"]: step["do"] for step in playbook["steps"]}
    if kind == "plan":
        plan = store.get_json(status.awaiting["artifact"])
        return {
            "kind": "plan",
            "plan": plan,
            "steps": [
                {
                    "step_id": step["step_id"],
                    "do": do.get(step["step_id"], ""),
                    "pinned": step["source"] == "playbook",
                    "actions": [describe_action(action) for action in step["actions"]],
                    "expect_text": step["expect_text"],
                }
                for step in plan["steps"]
            ],
        }

    # The story: each passage beside the step's last frame, with the words it has room for.
    story = store.get_json(status.awaiting["artifact"])
    windows = store.get_json(status.state["windows"])
    wps = runtime.context(run_id).config.target_wps
    facts = {fact["step_id"]: fact for fact in store.get_json(status.state["facts"])["steps"]} if status.state.get("facts") else {}
    frames: dict[str, str] = {}
    if status.state.get("keyframes"):
        for entry in store.get_json(status.state["keyframes"])["steps"]:
            last = (entry.get("frames") or [entry.get("before")])[-1]
            if last and last.get("image"):
                frames[entry["step_id"]] = last["image"]["path"]
    steps = []
    for passage in story["steps"]:
        window = windows.get(passage["step_id"], {"start": 0, "end": 0})
        budget = Budget.for_window(window["end"] - window["start"], wps)
        fact = facts.get(passage["step_id"], {})
        steps.append({
            "step_id": passage["step_id"],
            "do": do.get(passage["step_id"], ""),
            "text": passage["text"],
            "seconds": round(budget.seconds, 1),
            "target": budget.target,
            "limit": budget.limit,
            "keyframe": frames.get(passage["step_id"]),
            "happens": fact.get("happens"),
            "on_screen": fact.get("on_screen", []),
        })  # fmt: skip
    return {"kind": "story", "story": story, "steps": steps}

"""Rehearsal: an LLM performs one playbook step in the browser, and what it did becomes the plan."""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urljoin

from playwright.sync_api import Error as PlaywrightError

from demogen.driver.lockfile import ResolvedStep
from demogen.driver.playwright.session import ActionFailed, BrowserSession
from demogen.llm.agent_loop import Stop, ToolError, run_tool_loop
from demogen.llm.base import LLMProvider, tool
from demogen.playbook.actions import Action
from demogen.playbook.secrets import Redactor

MAX_TURNS = 12
SETTLE_MS = 300
# How long a hover is given to show something (a menu, a tooltip) before it counts as doing nothing.
HOVER_EFFECT_MS = 1200
HOVER_POLL_MS = 150
MAX_WAIT_S = 5.0

# Parts of a snapshot line that change without the page changing: element refs, focus, cursor.
_INCIDENTAL = re.compile(r"\s*\[(?:ref=[^\]]*|active|cursor=[^\]]*)\]")

SYSTEM = """\
You are rehearsing a product demo in a real browser. You are given one step at a time: what \
to do, and what should be true afterwards. Perform the step with the browser tools, then call \
step_done.

Every action you take is recorded and replayed later, on camera, for the finished demo video. \
So do exactly what the step asks, by the most direct route, the way a person presenting the \
product would: no exploratory clicks, nothing the step did not ask for, and no undoing.

Elements are addressed by the ref shown in the page snapshot, for example [ref=e12]. After \
your tool calls you get a fresh snapshot; refs from older snapshots no longer apply.

Only actions that do something go in the recording. When you just need to see the page again \
(a message appearing, a page still loading, a list updating), call `wait`: it records nothing. \
Never use hover, goto or any other action merely to refresh your view or to check something. \
The finished video already shows the pointer moving to each element you act on, so there is \
no need to hover before a click. Hover only where the step needs something that appears on \
hover, such as a menu. If an action does not take you where the step needs, look again with \
`wait` before acting again.

Text of the form ${secret:NAME} is a placeholder for a credential. Pass it to `fill` exactly \
as written; the real value is substituted outside your view.

When the outcome the step expects is visible on the page, call step_done with one to three \
short pieces of text, copied exactly from the current snapshot, that show the step worked. \
They are checked on every replay, so choose text that this step causes to appear. If the \
step cannot be done on this page, call step_failed and say why."""

_REF = {"ref": {"type": "string", "description": "The element's ref from the latest snapshot, e.g. e12"}}

TOOLS = [
    tool("click", "Click an element.", _REF),
    tool("fill", "Replace the contents of a text field.", {**_REF, "text": {"type": "string", "description": "The text to enter"}}),
    tool("press", "Press a key on the focused element.", {"key": {"type": "string", "description": "A key name such as Enter, Tab, Escape or ArrowDown"}}),
    tool("hover", "Move the pointer over an element.", _REF),
    tool("select_option", "Choose an option in a dropdown.", {**_REF, "value": {"type": "string", "description": "The option's label or value"}}),
    tool("goto", "Navigate to a URL. Relative URLs are resolved against the demo's base URL.", {"url": {"type": "string"}}),
    tool("wait", "Wait for the page to settle, then look at it again. Records nothing.", {
        "seconds": {"type": "number", "description": "How long to wait, from 0.5 to 5"},
    }),
    tool("step_done", "Finish the step once its expected outcome is visible.", {
        "evidence": {"type": "array", "items": {"type": "string"}, "description": "One to three short texts visible on the page now that show the step worked"},
    }),
    tool("step_failed", "Give up on the step because it cannot be done.", {"reason": {"type": "string"}}),
]  # fmt: skip


class StepUnresolved(Exception):
    """The model reported that the step cannot be done."""


class _Rehearsal:
    """Tool handlers for one step. Each successful action is appended to `actions`."""

    def __init__(self, session: BrowserSession):
        self.session = session
        self.actions: list[Action] = []

    def _page_state(self) -> str:
        """What the page shows, ignoring what changes without it changing."""
        try:
            return self.session.page.url + "\n" + _INCIDENTAL.sub("", self.session.page.aria_snapshot(mode="ai"))
        except PlaywrightError:
            return ""  # the page is mid-navigation: something is certainly changing

    def _on_element(self, kind: str, ref: str, text: str | None = None) -> str:
        try:
            element = self.session.by_ref(ref)
            target, label = self.session.durable_locators(ref, element)
            action = Action(type=kind, target=target, label=label, text=text)
            before = self._page_state() if kind == "hover" else None
            self.session.perform(action, element)
        except ActionFailed as error:
            raise ToolError(str(error)) from error
        if before is not None and not self._changed_since(before):
            return "Hovering there changed nothing on the page, so it is not part of the recording."
        self.actions.append(action)
        return "ok"

    def _changed_since(self, before: str) -> bool:
        """Whether the page shows something new within a moment: a hover menu can take a little while to open."""
        for _ in range(HOVER_EFFECT_MS // HOVER_POLL_MS):
            if self._page_state() != before:
                return True
            self.session.page.wait_for_timeout(HOVER_POLL_MS)
        return self._page_state() != before

    def _on_page(self, action: Action) -> str:
        try:
            self.session.perform(action)
        except ActionFailed as error:
            raise ToolError(str(error)) from error
        self.actions.append(action)
        return "ok"

    def click(self, ref: str) -> str:
        return self._on_element("click", ref)

    def hover(self, ref: str) -> str:
        return self._on_element("hover", ref)

    def fill(self, ref: str, text: str) -> str:
        return self._on_element("fill", ref, text)

    def select_option(self, ref: str, value: str) -> str:
        return self._on_element("select", ref, value)

    def press(self, key: str) -> str:
        return self._on_page(Action(type="press", key=key))

    def goto(self, url: str) -> str:
        if urljoin(self.session.target.base_url, url) == self.session.page.url:
            return "The page is already at that address, so nothing was done or recorded."
        return self._on_page(Action(type="goto", url=url))

    def wait(self, seconds: float) -> str:
        self.session.page.wait_for_timeout(min(max(seconds, 0.5), MAX_WAIT_S) * 1000)
        return "waited"

    def step_done(self, evidence: list[str]) -> Stop:
        if not 1 <= len(evidence) <= 3:
            raise ToolError("give one to three pieces of evidence")
        missing = [text for text in evidence if not self._visible(text)]
        if missing:
            raise ToolError(f"not visible on the page as written: {missing}. Copy text exactly from the snapshot.")
        return Stop(evidence)

    def step_failed(self, reason: str) -> str:
        raise StepUnresolved(reason)

    def _visible(self, text: str) -> bool:
        try:
            return self.session.page.get_by_text(text).first.is_visible()
        except PlaywrightError:
            return False


def resolve_step(
    session: BrowserSession,
    llm: LLMProvider,
    redactor: Redactor,
    *,
    step_id: str,
    do: str,
    expect: str | None = None,
    data: dict[str, Any] | None = None,
    done: list[str] = (),
) -> ResolvedStep:
    """Have the model perform one step on the session's current page. Raises `StepUnresolved`."""

    def observe() -> str:
        session.page.wait_for_timeout(SETTLE_MS)
        return f"Page now ({session.page.url}):\n{redactor.redact(session.snapshot())}"

    brief = [f"Step: {step_id}", f"Do: {do}"]
    if data:
        brief.append(f"Data: {json.dumps(data)}")
    brief.append(f"Expect afterwards: {expect or 'nothing specific; finish once the action has visibly taken effect'}")
    if done:
        brief.append("Already done earlier in this demo:\n" + "\n".join(f"- {item}" for item in done))
    brief.append(observe())

    rehearsal = _Rehearsal(session)
    handlers = {spec["function"]["name"]: getattr(rehearsal, spec["function"]["name"]) for spec in TOOLS}
    messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": "\n\n".join(brief)}]
    evidence = run_tool_loop(llm, messages, TOOLS, handlers, max_turns=MAX_TURNS, observe=observe)
    return ResolvedStep(
        step_id=step_id,
        source="llm",
        actions=rehearsal.actions,
        expect_text=evidence,
    )

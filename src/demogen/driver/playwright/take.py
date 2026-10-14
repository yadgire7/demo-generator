"""The take: replay the plan on camera at a human pace, logging everything the editor will need."""

from __future__ import annotations

import json
import random
from collections.abc import Callable
from pathlib import Path
from typing import Any

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Locator as PageLocator

from demogen.driver.lockfile import Plan
from demogen.driver.playwright.humanize import SAMPLE_HZ, mouse_path
from demogen.driver.playwright.session import ActionFailed, BrowserSession
from demogen.playbook.actions import Action
from demogen.playbook.schema import Playbook
from demogen.playbook.secrets import resolve_secrets
from demogen.recorder.cdp_screencast import CdpScreencastRecorder

LEAD_IN_MS = 800  # the opening page, before anything moves
TAIL_MS = 800
ACTION_SETTLE_MS = 350  # lets the page react before the next action
MIN_STEP_REST_S = 0.6  # every step ends on a beat of stillness, even with no dwell
TYPE_DELAY_MS = 65
EXPECT_TIMEOUT_MS = 5000
SCROLL_POLL_MS = 100
SCROLL_SETTLE_POLLS = 30


class StepFailed(Exception):
    def __init__(self, step_id: str, reason: str):
        super().__init__(f"step {step_id!r}: {reason}")
        self.step_id = step_id
        self.reason = reason


class EventLog:
    """events.jsonl: one JSON object per line, stamped in seconds on the recorder's clock."""

    def __init__(self, clock: Callable[[], float]):
        self._clock = clock
        self._events: list[dict[str, Any]] = []

    def add(self, kind: str, **fields: Any) -> None:
        self._events.append({"t": round(self._clock(), 3), "type": kind, **fields})

    def dump(self) -> bytes:
        return "".join(json.dumps(event) + "\n" for event in self._events).encode()


def run_take(session: BrowserSession, playbook: Playbook, plan: Plan, video: Path, *, fps: int, crf: int) -> EventLog:
    """Record one take to `video`. Raises `StepFailed` (and discards the recording) if a step breaks."""
    page = session.page
    viewport = session.target.viewport
    session.open()
    recorder = CdpScreencastRecorder(fps=fps, crf=crf)
    recorder.start(session.context, page, video, session.video_size)
    events = EventLog(recorder.now)
    # Seeded per playbook, so the same plan always moves the pointer the same way.
    take = _Take(session, events, random.Random(playbook.name))
    try:
        events.add("meta", viewport=[viewport.width, viewport.height], video_size=list(session.video_size), fps=fps)
        take.place_pointer(viewport.width / 2, viewport.height / 2)
        page.wait_for_timeout(LEAD_IN_MS)
        dwell = {step.id: step.dwell for step in playbook.steps}
        for step in plan.steps:
            events.add("step_start", step_id=step.step_id)
            try:
                for action in step.actions:
                    take.perform(action, step.step_id)
                for text in step.expect_text:
                    try:
                        page.get_by_text(text).first.wait_for(state="visible", timeout=EXPECT_TIMEOUT_MS)
                    except PlaywrightError:
                        # The actions ran, but the page did not end up as it did in rehearsal.
                        raise ActionFailed(f"the page did not show {text!r}, which it showed at this point in rehearsal") from None
            except ActionFailed as error:
                raise StepFailed(step.step_id, str(error)) from error
            except PlaywrightError as error:
                raise StepFailed(step.step_id, error.message.splitlines()[0]) from error
            # From here to the step's end the screen is at rest, showing the step's result.
            events.add("settled", step_id=step.step_id)
            page.wait_for_timeout(max(dwell.get(step.step_id, 0), MIN_STEP_REST_S) * 1000)
            events.add("step_end", step_id=step.step_id)
        page.wait_for_timeout(TAIL_MS)
    except BaseException:
        recorder.abort()
        raise
    events.add("end")
    recorder.stop()
    return events


class _Take:
    def __init__(self, session: BrowserSession, events: EventLog, rng: random.Random):
        self.session = session
        self.page = session.page
        self.events = events
        self.rng = rng
        self.pointer = (0.0, 0.0)

    def place_pointer(self, x: float, y: float) -> None:
        self.page.mouse.move(x, y)
        self.pointer = (x, y)
        self.events.add("cursor", x=x, y=y)

    def _glide_to(self, element: PageLocator) -> tuple[float, float]:
        """Bring the element into view and move the pointer to its centre, as a hand would."""
        viewport = self.session.target.viewport
        box = element.bounding_box()
        if box is None:
            raise ActionFailed("the element is not visible")
        if box["y"] < 0 or box["y"] + box["height"] > viewport.height or box["x"] < 0 or box["x"] + box["width"] > viewport.width:
            box = self._scroll_to(element)
        x = min(max(box["x"] + box["width"] / 2, 1), viewport.width - 1)
        y = min(max(box["y"] + box["height"] / 2, 1), viewport.height - 1)
        for px, py in mouse_path(self.pointer, (x, y), self.rng):
            self.page.mouse.move(px, py)
            self.events.add("cursor", x=round(px, 1), y=round(py, 1))
            self.page.wait_for_timeout(1000 / SAMPLE_HZ)
        self.pointer = (x, y)
        return x, y

    def _scroll_to(self, element: PageLocator) -> dict[str, float]:
        """Scroll the element to mid-screen smoothly, and wait for the page to come to rest."""
        element.evaluate("el => el.scrollIntoView({ behavior: 'smooth', block: 'center', inline: 'center' })")
        box = element.bounding_box()
        for _ in range(SCROLL_SETTLE_POLLS):
            self.page.wait_for_timeout(SCROLL_POLL_MS)
            previous, box = box, element.bounding_box()
            if box == previous:
                break
        if box is None:
            raise ActionFailed("the element is not visible")
        return box

    def perform(self, action: Action, step_id: str) -> None:
        if action.type in ("goto", "press"):
            self.events.add("action", step_id=step_id, action=action.type, detail=action.url or action.key)
            self.session.perform(action)
        else:
            element = self.session.locate(action)
            x, y = self._glide_to(element)
            self.events.add("action", step_id=step_id, action=action.type, label=action.label, bbox=element.bounding_box())
            if action.type == "hover":
                element.hover()
            elif action.type == "select":
                element.select_option(resolve_secrets(action.text))
            else:
                self.events.add("click", step_id=step_id, x=round(x, 1), y=round(y, 1))
                element.click()
                if action.type == "fill":
                    # Type over whatever is there, as a person would: select it all, then type.
                    # Clearing the field first is not the same thing: a field that refills
                    # itself when emptied (a quantity snapping back to 1) would end up with both.
                    element.press("ControlOrMeta+a")
                    element.press_sequentially(resolve_secrets(action.text), delay=TYPE_DELAY_MS)
            self.page.wait_for_load_state("load")
        self.page.wait_for_timeout(ACTION_SETTLE_MS)

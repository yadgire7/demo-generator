"""A browser on the demo's target, and the plain (unrecorded, unhurried) way to perform an action."""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urljoin

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Locator as PageLocator
from playwright.sync_api import sync_playwright

from demogen.playbook.actions import Action, Locator
from demogen.playbook.schema import Target
from demogen.playbook.secrets import resolve_secrets

FIND_TIMEOUT_MS = 5000
FALLBACK_TIMEOUT_MS = 1000

# A snapshot line such as:   - button "New invoice" [ref=e12] [cursor=pointer]:
_SNAPSHOT_LINE = re.compile(r"""^\s*-\s+'?(?P<role>[a-z]+)(?:\s+"(?P<name>(?:[^"\\]|\\.)*)")?[^\n]*?\[ref=(?P<ref>\w+)\]""", re.M)

# Shortest stable CSS path: stop at a unique test id or id, otherwise walk up by tag position.
_CSS_PATH = """(el) => {
  const unique = (sel) => document.querySelectorAll(sel).length === 1;
  const parts = [];
  for (let node = el; node && node.nodeType === 1 && node !== document.documentElement; node = node.parentElement) {
    const testId = node.getAttribute('data-testid');
    if (testId && unique(`[data-testid=${JSON.stringify(testId)}]`)) { parts.unshift(`[data-testid=${JSON.stringify(testId)}]`); break; }
    if (node.id && unique('#' + CSS.escape(node.id))) { parts.unshift('#' + CSS.escape(node.id)); break; }
    const tag = node.tagName.toLowerCase();
    const twins = node.parentElement ? [...node.parentElement.children].filter((c) => c.tagName === node.tagName) : [node];
    parts.unshift(twins.length > 1 ? `${tag}:nth-of-type(${twins.indexOf(node) + 1})` : tag);
  }
  return parts.join(' > ');
}"""


class ActionFailed(Exception):
    pass


def snapshot_elements(snapshot: str) -> dict[str, tuple[str, str | None]]:
    """ref -> (role, accessible name) for every referenced element in an accessibility snapshot."""
    elements = {}
    for match in _SNAPSHOT_LINE.finditer(snapshot):
        name = match.group("name")
        elements[match.group("ref")] = (match.group("role"), json.loads(f'"{name}"') if name is not None else None)
    return elements


class BrowserSession:
    def __init__(self, target: Target, *, headless: bool = True, storage_state: dict[str, Any] | None = None):
        self.target = target
        self.headless = headless
        self.storage_state = storage_state
        self.last_snapshot = ""

    def __enter__(self) -> BrowserSession:
        viewport = self.target.viewport
        self._playwright = sync_playwright().start()
        # The flag makes the compositor render at device pixels, so screen capture is as sharp
        # as the scale factor promises. The context option alone only affects layout.
        self._browser = self._playwright.chromium.launch(
            headless=self.headless, args=[f"--force-device-scale-factor={viewport.device_scale_factor:g}"]
        )
        self.context = self._browser.new_context(
            viewport={"width": viewport.width, "height": viewport.height},
            device_scale_factor=viewport.device_scale_factor,
            storage_state=self.storage_state,
        )
        self.page = self.context.new_page()
        return self

    def __exit__(self, *exc: object) -> None:
        self._browser.close()
        self._playwright.stop()

    @property
    def video_size(self) -> tuple[int, int]:
        viewport = self.target.viewport
        return round(viewport.width * viewport.device_scale_factor), round(viewport.height * viewport.device_scale_factor)

    def open(self) -> None:
        self.page.goto(self.target.base_url, wait_until="load")

    def snapshot(self) -> str:
        self.last_snapshot = self.page.aria_snapshot(mode="ai")
        return self.last_snapshot

    def by_ref(self, ref: str) -> PageLocator:
        """The element a snapshot ref points at. Raises `ActionFailed` for a stale or unknown ref."""
        locator = self.page.locator(f"aria-ref={ref}")
        try:
            found = locator.count() == 1
        except PlaywrightError:
            found = False
        if not found:
            raise ActionFailed(f"ref {ref} is not in the current page snapshot")
        return locator

    def durable_locators(self, ref: str, element: PageLocator) -> tuple[list[Locator], str]:
        """Ways to find this element again in a fresh browser, most durable first, plus a label."""
        role, name = snapshot_elements(self.last_snapshot).get(ref, ("generic", None))
        candidates: list[Locator] = []
        if name and role != "generic":
            matches = self.page.get_by_role(role, name=name, exact=True)  # type: ignore[arg-type]
            index = matches.evaluate_all("(els, target) => els.indexOf(target)", element.element_handle())
            if index >= 0:
                candidates.append(Locator(by="role", role=role, name=name, nth=index if matches.count() > 1 else None))
        candidates.append(Locator(by="css", selector=element.evaluate(_CSS_PATH)))
        return candidates, f'{role} "{name}"' if name else role

    def locate(self, action: Action) -> PageLocator:
        """Find the action's element through its first candidate that matches exactly one element."""
        timeout = FIND_TIMEOUT_MS
        for candidate in action.target:
            if candidate.by == "role":
                kwargs = {"name": candidate.name, "exact": True} if candidate.name else {}
                locator = self.page.get_by_role(candidate.role, **kwargs)  # type: ignore[arg-type]
            else:
                locator = self.page.locator(candidate.selector)
            try:
                locator.first.wait_for(state="visible", timeout=timeout)
            except PlaywrightError:
                timeout = FALLBACK_TIMEOUT_MS
                continue
            if candidate.nth is not None:
                return locator.nth(candidate.nth)
            if locator.count() == 1:
                return locator
            timeout = FALLBACK_TIMEOUT_MS
        raise ActionFailed(f"could not find {action.label or 'the element'} to {action.type}")

    def perform(self, action: Action, element: PageLocator | None = None) -> None:
        """Carry out an action at once. Raises `ActionFailed` if it cannot be done."""
        try:
            if action.type == "goto":
                self.page.goto(urljoin(self.target.base_url, action.url), wait_until="load")
            elif action.type == "press":
                self.page.keyboard.press(action.key)
            else:
                element = element or self.locate(action)
                if action.type == "click":
                    element.click()
                elif action.type == "hover":
                    element.hover()
                elif action.type == "fill":
                    element.fill(resolve_secrets(action.text))
                elif action.type == "select":
                    element.select_option(resolve_secrets(action.text))
            self.page.wait_for_load_state("load")
        except PlaywrightError as error:
            raise ActionFailed(f"{action.describe()} failed: {error.message.splitlines()[0]}") from error

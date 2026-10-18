"""Fail points: each time an action, a replay or a repair goes wrong, with the page as it was.

Every fail point is kept in the run's `failures/` folder: a JSON record, a screenshot of the
page at that moment, its accessibility snapshot and, for the model's own mistakes, the snapshot
the model was looking at when it chose the action. They say why a run stopped. Gathered across
runs (`demogen failures`), they are examples of computer-use mistakes: what the agent was asked,
what it saw, what it did, what went wrong, and whether it recovered.

A record (schema `demogen.failure/1`):

    id, run_id, time, phase       rehearsal | take | repair | setup
    kind                          what went wrong (see KINDS)
    message                       what the agent, or the person reading the log, was told
    task                          the demo: its name and start address
    step                          the step: id, position, what to do, what should show after
    done                          what earlier steps did, in words
    action                        the browser action concerned (type, locators, label, text)
    tool_call                     the model's call as made: tool name and arguments
    model                         the model that made it, for the model's own mistakes
    attempt                       which take or repair this was
    files                         run-relative paths: screenshot, snapshot, observation
    page                          url and title at that moment
    step_outcome                  how the step ended: resolved (the agent recovered), unresolved,
                                  failed, or null while it is still under way

Secrets are kept out of the text: snapshots are passed through the playbook's redactor, and
typed credentials appear only as ${secret:NAME}. A screenshot shows the screen as it was.
"""

from __future__ import annotations

import json
import logging
import secrets
from datetime import datetime, timezone
from typing import Any

from opentelemetry import trace

from demogen.playbook.schema import Playbook
from demogen.playbook.secrets import Redactor
from demogen.store.artifacts import ArtifactStore

log = logging.getLogger("demogen.failures")

SCHEMA = "demogen.failure/1"
FOLDER = "failures"
SCREENSHOT_TIMEOUT_MS = 5000

# What went wrong, by phase.
KINDS = {
    # The model's own calls, in rehearsal, repair and setup.
    "bad_ref": "acted on an element that is not in the current snapshot",
    "element_not_found": "the element could not be found on the page",
    "action_failed": "the browser could not carry out the action",
    "evidence_not_visible": "claimed the step was done with text that is not on the page",
    "fragile_target": "chose an element that could not be found again on a later visit",
    "no_effect": "took an action that changed nothing (not recorded)",
    "bad_arguments": "called a tool with wrong or missing arguments",
    "unknown_tool": "called a tool that does not exist",
    "step_unresolved": "gave up on the step",
    "turn_limit": "did not finish the step within its turns",
    # Replaying a plan: the take, and the replay of earlier steps before a repair.
    "expectation_missed": "the page did not show what rehearsal saw at this point",
}


def model_name(config: Any) -> str | None:
    model = config.models.get("planner")
    return f"{model.provider}/{model.model}" if model else None


class FailureLog:
    """Writes a run's fail points. One per stage execution; each record is written as it happens."""

    def __init__(self, store: ArtifactStore, run_id: str, playbook: Playbook, redactor: Redactor, model: str | None = None):
        self.store = store
        self.run_id = run_id
        self.playbook = playbook
        self.redactor = redactor
        self.model = model
        self._batch = f"{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"
        self._count = 0

    def _step(self, step_id: str) -> dict[str, Any]:
        for index, step in enumerate(self.playbook.steps):
            if step.id == step_id:
                return {"id": step.id, "index": index, "do": step.do, "expect": step.expect, "data": step.data or None}
        for index, step in enumerate(self.playbook.setup.steps, start=1):
            if step_id == f"setup-{index}":
                return {"id": step_id, "index": index - 1, "do": step.do, "expect": None, "data": None}
        return {"id": step_id, "index": None, "do": None, "expect": None, "data": None}

    def record(
        self,
        session: Any,
        *,
        phase: str,
        kind: str,
        step_id: str,
        message: str,
        action: Any = None,
        tool_call: dict[str, Any] | None = None,
        observation: str | None = None,
        attempt: dict[str, Any] | None = None,
        agent: bool = False,
        step_outcome: str | None = None,
    ) -> dict[str, Any]:
        """Keep one fail point, with a picture of `session`'s page as it is now."""
        self._count += 1
        record_id = f"{self._batch}-{self._count:03d}"
        stem = f"{FOLDER}/{record_id}-{phase}-{step_id}"
        step = self._step(step_id)
        record: dict[str, Any] = {
            "schema": SCHEMA,
            "id": record_id,
            "run_id": self.run_id,
            "time": datetime.now(timezone.utc).isoformat(),
            "phase": phase,
            "kind": kind,
            "message": self.redactor.redact(message),
            "task": {"name": self.playbook.name, "base_url": self.playbook.target.base_url},
            "step": step,
            "done": [earlier.do for earlier in self.playbook.steps[: step["index"]]] if phase != "setup" and step["index"] else [],
            "action": action.model_dump(mode="json", exclude_none=True) if hasattr(action, "model_dump") else action,
            "tool_call": tool_call,
            "model": self.model if agent else None,
            "attempt": attempt,
            "files": {},
            "page": {},
            "step_outcome": step_outcome,
        }
        if observation:
            self.store.put_bytes(f"{stem}.observation.txt", self.redactor.redact(observation).encode())
            record["files"]["observation"] = f"{stem}.observation.txt"
        self._capture_page(session, stem, record)
        self._write(stem, record)
        log.warning("fail point in run %s, %s step %s: %s (%s)", self.run_id, phase, step_id, kind, record["message"])
        trace.get_current_span().add_event(
            "demogen.failure", {"demogen.failure.phase": phase, "demogen.failure.kind": kind, "demogen.failure.step": step_id}
        )
        record["_stem"] = stem
        return record

    def _capture_page(self, session: Any, stem: str, record: dict[str, Any]) -> None:
        """The page's address, title, screenshot and snapshot. A page that cannot be read is noted, not fatal."""
        page = getattr(session, "page", None)
        if page is None:
            return
        try:
            record["page"] = {"url": page.url, "title": page.title()}
            self.store.put_bytes(f"{stem}.png", page.screenshot(type="png", timeout=SCREENSHOT_TIMEOUT_MS))
            record["files"]["screenshot"] = f"{stem}.png"
            snapshot = self.redactor.redact(page.aria_snapshot(mode="ai"))
            self.store.put_bytes(f"{stem}.snapshot.txt", snapshot.encode())
            record["files"]["snapshot"] = f"{stem}.snapshot.txt"
        except Exception as error:  # a closed or navigating page: keep what there is
            record["page"]["error"] = f"could not capture the page: {error}".splitlines()[0]

    def _write(self, stem: str, record: dict[str, Any]) -> None:
        self.store.put_json(f"{stem}.json", {key: value for key, value in record.items() if not key.startswith("_")})

    def settle(self, records: list[dict[str, Any]], outcome: str) -> None:
        """Say how the step these fail points belong to ended, now that it has."""
        for record in records:
            record["step_outcome"] = outcome
            self._write(record["_stem"], record)

    def journal(self, phase: str, step_id: str, attempt: dict[str, Any] | None = None) -> StepJournal:
        return StepJournal(self, phase, step_id, attempt)


class StepJournal:
    """The fail points of one agent working on one step. They learn the step's outcome when it ends."""

    def __init__(self, log: FailureLog, phase: str, step_id: str, attempt: dict[str, Any] | None):
        self.log = log
        self.phase = phase
        self.step_id = step_id
        self.attempt = attempt
        self.records: list[dict[str, Any]] = []

    def note(self, session: Any, kind: str, message: str, **fields: Any) -> None:
        self.records.append(self.log.record(session, phase=self.phase, kind=kind, step_id=self.step_id, message=message, attempt=self.attempt, agent=True, **fields))

    def close(self, outcome: str) -> None:
        self.log.settle(self.records, outcome)


def read_failures(store: ArtifactStore) -> list[dict[str, Any]]:
    """A run's fail points, oldest first."""
    records = []
    for path in store.list_prefix(FOLDER):
        if path.endswith(".json"):
            try:
                records.append(json.loads(store.get_bytes(path)))
            except (FileNotFoundError, json.JSONDecodeError):
                continue
    return sorted(records, key=lambda record: (record["time"], record["id"]))

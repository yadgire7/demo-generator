"""Start, inspect, approve, resume and rerun runs. The CLI talks to this, never to LangGraph."""

from __future__ import annotations

import secrets
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import yaml
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from demogen.config import Config
from demogen.driver.lockfile import Plan
from demogen.exec.base import StageExecutor
from demogen.exec.local import LocalExecutor
from demogen.graph.build import RERUNNABLE, STATE_VERSION, WIRING, build_graph
from demogen.playbook.loader import merge_intent, parse_playbook
from demogen.playbook.schema import Playbook
from demogen.errors import DemogenError
from demogen.stages import StageContext
from demogen.store.artifacts import ArtifactRef, LocalArtifactStore
from demogen.store.cache import StageCache
from demogen.story.schema import Story

CONFIG_FILE = "config.resolved.yaml"
# What an approval may replace: kind -> (artifact path, schema)
EDITABLE = {"plan": ("actions.lock.json", Plan), "story": ("story.json", Story)}

Phase = Literal["awaiting_approval", "incomplete", "done"]


class RunError(DemogenError):
    pass


@dataclass
class RunStatus:
    run_id: str
    phase: Phase
    next: tuple[str, ...] = ()
    awaiting: dict[str, Any] | None = None
    state: dict[str, Any] = field(default_factory=dict)


class Runtime:
    def __init__(self, runs_dir: Path, executor: StageExecutor | None = None):
        self.runs_dir = runs_dir
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        self._contexts: dict[str, StageContext] = {}
        self._conn = sqlite3.connect(self.runs_dir / "checkpoints.sqlite", check_same_thread=False)
        self.graph = build_graph(executor or LocalExecutor(), self._context).compile(
            checkpointer=SqliteSaver(self._conn)
        )

    def close(self) -> None:
        self._conn.close()

    def _context(self, run_id: str) -> StageContext:
        if run_id not in self._contexts:
            run_dir = self.runs_dir / run_id
            if not (run_dir / CONFIG_FILE).is_file():
                raise RunError(f"no run named {run_id!r} in {self.runs_dir}")
            config = Config.model_validate(yaml.safe_load((run_dir / CONFIG_FILE).read_text()) or {})
            self._contexts[run_id] = StageContext(run_id, LocalArtifactStore(run_dir), config)
        return self._contexts[run_id]

    @staticmethod
    def _thread(run_id: str) -> dict[str, Any]:
        return {"configurable": {"thread_id": run_id}}

    def _invoke(self, run_id: str, graph_input: Any, config: dict[str, Any] | None = None) -> None:
        try:
            self.graph.invoke(graph_input, config or self._thread(run_id))
        except DemogenError as error:
            raise RunError(f"{error}\nThe run is saved. Fix the cause, then: demogen resume {run_id}") from error

    def start(self, playbook: Path, intent: Path | None, config: Config, run_id: str | None = None) -> RunStatus:
        parse_playbook(playbook.read_text())  # fail before a run exists, not inside it
        run_id = run_id or f"{playbook.stem}-{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"
        run_dir = self.runs_dir / run_id
        if run_dir.exists():
            raise RunError(f"run {run_id!r} already exists")
        store = LocalArtifactStore(run_dir)
        store.put_bytes(CONFIG_FILE, yaml.safe_dump(config.model_dump(mode="json")).encode())
        initial = {
            "state_version": STATE_VERSION,
            "run_id": run_id,
            "playbook_src": store.put_bytes("playbook.yaml", playbook.read_bytes()),
            "intent_src": store.put_bytes("intent.md", intent.read_bytes()) if intent else None,
        }
        self._invoke(run_id, initial)
        return self.status(run_id)

    def status(self, run_id: str) -> RunStatus:
        self._context(run_id)
        snapshot = self.graph.get_state(self._thread(run_id))
        if snapshot.interrupts:
            return RunStatus(run_id, "awaiting_approval", snapshot.next, snapshot.interrupts[0].value, snapshot.values)
        if snapshot.next or not snapshot.values:
            return RunStatus(run_id, "incomplete", snapshot.next, None, snapshot.values)
        return RunStatus(run_id, "done", (), None, snapshot.values)

    def approve(self, run_id: str, edit: Path | None = None) -> RunStatus:
        status = self.status(run_id)
        if status.phase != "awaiting_approval":
            raise RunError(f"run {run_id} is not waiting for approval (it is {status.phase})")
        decision: dict[str, Any] = {"artifact": None}
        if edit is not None:
            decision["artifact"] = self._store_edit(run_id, status.awaiting["kind"], edit)
        self._invoke(run_id, Command(resume=decision))
        return self.status(run_id)

    def _store_edit(self, run_id: str, kind: str, edit: Path) -> ArtifactRef:
        """Validate an author's edited plan or story and store it in place of the generated one."""
        ctx = self._context(run_id)
        path, schema = EDITABLE[kind]
        edited = schema.model_validate(yaml.safe_load(edit.read_text()))
        playbook = Playbook.model_validate(ctx.store.get_json("playbook.json"))
        expected = [step.id for step in playbook.steps]
        if [step.step_id for step in edited.steps] != expected:
            raise RunError(f"the edited {kind} must cover exactly the playbook's steps, in order: {expected}")
        if kind == "story" and any(not step.text.strip() for step in edited.steps):
            raise RunError("every passage in the edited story needs some words to say")
        return ctx.store.put_json(path, edited.model_dump(mode="json"))

    def resume(self, run_id: str) -> RunStatus:
        """Continue an incomplete run (after a crash or a failed stage) from its last checkpoint."""
        status = self.status(run_id)
        if status.phase == "incomplete":
            self._invoke(run_id, None)
        return self.status(run_id)

    def rerun(self, run_id: str, from_node: str, intent: Path | None = None) -> RunStatus:
        """Fork the run at the point where `from_node` was about to start, and run forward again."""
        if from_node not in RERUNNABLE:
            raise RunError(f"cannot rerun from {from_node!r}; choose one of: {', '.join(RERUNNABLE)}")
        ctx = self._context(run_id)
        fork = next(
            (s for s in self.graph.get_state_history(self._thread(run_id)) if s.next == (from_node,)), None
        )
        if fork is None:
            raise RunError(f"run {run_id} has not reached {from_node} yet")
        config = fork.config
        if intent is not None:
            playbook = parse_playbook(ctx.store.get_bytes("playbook.yaml").decode())
            merged = merge_intent(playbook, intent.read_text()).model_dump(mode="json")
            update = {
                "intent_src": ctx.store.put_bytes("intent.md", intent.read_bytes()),
                "intent": ctx.store.put_json("intent.json", merged),
            }
            config = self.graph.update_state(config, update)
        if from_node in WIRING:
            # The stage's cached result would otherwise answer straight away with the old output.
            StageCache(ctx.store).invalidate(from_node)
        self._invoke(run_id, None, config)
        return self.status(run_id)

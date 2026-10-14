"""Start, inspect, approve, resume and rerun runs. The CLI and the service talk to this, never to LangGraph.

Every change to a run happens in two parts. `prepare_*` checks the request and stores what it
brings (quick; where the caller is). `advance` then runs the pipeline forward until it next
pauses or finishes (slow; in the same process for the command line, in a worker for the service).
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import yaml
from langgraph.types import Command

from demogen import telemetry
from demogen.config import Config
from demogen.driver.lockfile import Plan
from demogen.errors import DemogenError
from demogen.exec.base import StageExecutor
from demogen.exec.local import LocalExecutor
from demogen.graph.build import RERUNNABLE, STATE_VERSION, WIRING, build_graph
from demogen.graph.checkpoint import open_checkpointer
from demogen.playbook.loader import merge_intent, parse_playbook
from demogen.playbook.schema import Playbook
from demogen.stages import StageContext
from demogen.store.artifacts import ArtifactRef
from demogen.store.cache import StageCache
from demogen.store.stores import CONFIG_FILE, LocalStores, Stores
from demogen.story.schema import Story

# What an approval may replace: kind -> (artifact path, schema)
EDITABLE = {"plan": ("actions.lock.json", Plan), "story": ("story.json", Story)}

Phase = Literal["awaiting_approval", "incomplete", "done"]
# What `advance` is asked to do: {"kind": "start" | "approve" | "resume" | "rerun", ...}
Step = dict[str, Any]


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
    def __init__(self, stores: Stores, checkpoints: str | Path, executor: StageExecutor | None = None):
        self.stores = stores
        self._contexts: dict[str, StageContext] = {}
        checkpointer, self._close = open_checkpointer(checkpoints)
        self.graph = build_graph(executor or LocalExecutor(), self.context).compile(checkpointer=checkpointer)

    @classmethod
    def local(cls, runs_dir: Path, executor: StageExecutor | None = None) -> Runtime:
        """Everything on this machine: a folder per run and a SQLite file of checkpoints beside them."""
        stores = LocalStores(runs_dir)
        return cls(stores, runs_dir / "checkpoints.sqlite", executor)

    def close(self) -> None:
        self._close()

    def context(self, run_id: str) -> StageContext:
        if run_id not in self._contexts:
            if not self.stores.exists(run_id):
                raise RunError(f"no run named {run_id!r} in {self.stores.describe()}")
            store = self.stores.for_run(run_id)
            config = Config.model_validate(yaml.safe_load(store.get_bytes(CONFIG_FILE)) or {})
            self._contexts[run_id] = StageContext(run_id, store, config)
        return self._contexts[run_id]

    @staticmethod
    def _thread(run_id: str) -> dict[str, Any]:
        return {"configurable": {"thread_id": run_id}}

    def status(self, run_id: str) -> RunStatus:
        self.context(run_id)
        snapshot = self.graph.get_state(self._thread(run_id))
        if snapshot.interrupts:
            return RunStatus(run_id, "awaiting_approval", snapshot.next, snapshot.interrupts[0].value, snapshot.values)
        if snapshot.next or not snapshot.values:
            return RunStatus(run_id, "incomplete", snapshot.next, None, snapshot.values)
        return RunStatus(run_id, "done", (), None, snapshot.values)

    def forget(self, run_id: str) -> None:
        """Delete a run's checkpoints. Its artifacts are untouched, but it can no longer be resumed or rerun."""
        self.graph.checkpointer.delete_thread(run_id)
        self._contexts.pop(run_id, None)

    # Preparing: check the request and store what it brings.

    def prepare_start(
        self,
        playbook: bytes | None,
        intent: bytes | None,
        config: Config,
        run_id: str | None = None,
        name: str = "run",
        *,
        prompt: str | None = None,
        url: str | None = None,
    ) -> tuple[str, Step]:
        """Begin a run from a playbook, or from a `prompt` describing the demo for one to be drafted."""
        if (playbook is None) == (not prompt or not prompt.strip()):
            raise RunError("start a run with either a playbook or a prompt describing the demo, not both")
        if playbook is not None:
            parse_playbook(playbook.decode())  # fail before a run exists, not inside it
        run_id = run_id or f"{name}-{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"
        if self.stores.exists(run_id):
            raise RunError(f"run {run_id!r} already exists")
        store = self.stores.for_run(run_id)
        store.put_bytes(CONFIG_FILE, yaml.safe_dump(config.model_dump(mode="json")).encode())
        state: dict[str, Any] = {
            "state_version": STATE_VERSION,
            "run_id": run_id,
            "intent_src": store.put_bytes("intent.md", intent) if intent else None,
        }
        if playbook is not None:
            state["playbook_src"] = store.put_bytes("playbook.yaml", playbook)
        else:
            state["prompt_src"] = store.put_bytes("prompt.md", prompt.strip().encode())
            state["target_url"] = url
        return run_id, {"kind": "start", "state": state}

    def prepare_approval(self, run_id: str, edit: str | None = None, feedback: str | None = None) -> Step:
        """Approve what the run waits on, replace it with an edited version, or (for a drafted playbook) ask for changes."""
        status = self.status(run_id)
        if status.phase != "awaiting_approval":
            raise RunError(f"run {run_id} is not waiting for approval (it is {status.phase})")
        kind = status.awaiting["kind"]
        if edit is not None and feedback:
            raise RunError("send either an edited file or feedback, not both")
        if feedback and kind != "playbook":
            raise RunError(f"feedback in words is only taken for a drafted playbook; to change the {kind}, send an edited copy of it")
        artifact = self._store_edit(run_id, kind, edit) if edit is not None else None
        return {"kind": "approve", "decision": {"artifact": artifact, "feedback": feedback.strip() if feedback else None}}

    def _store_edit(self, run_id: str, kind: str, edit: str) -> ArtifactRef:
        """Validate an author's edited plan or story and store it in place of the generated one."""
        ctx = self.context(run_id)
        if kind == "playbook":
            parse_playbook(edit)  # the author's own file is kept as they wrote it, once it is known to be valid
            return ctx.store.put_bytes("playbook.yaml", edit.encode())
        path, schema = EDITABLE[kind]
        edited = schema.model_validate(yaml.safe_load(edit))
        playbook = Playbook.model_validate(ctx.store.get_json("playbook.json"))
        expected = [step.id for step in playbook.steps]
        if [step.step_id for step in edited.steps] != expected:
            raise RunError(f"the edited {kind} must cover exactly the playbook's steps, in order: {expected}")
        if kind == "story" and any(not step.text.strip() for step in edited.steps):
            raise RunError("every passage in the edited story needs some words to say")
        return ctx.store.put_json(path, edited.model_dump(mode="json"))

    def prepare_resume(self, run_id: str) -> Step | None:
        """The step that continues an incomplete run, or None if there is nothing to continue."""
        return {"kind": "resume"} if self.status(run_id).phase == "incomplete" else None

    def prepare_rerun(self, run_id: str, from_node: str, intent: str | None = None) -> Step:
        if from_node not in RERUNNABLE:
            raise RunError(f"cannot rerun from {from_node!r}; choose one of: {', '.join(RERUNNABLE)}")
        self.context(run_id)
        if self._fork_point(run_id, from_node) is None:
            raise RunError(f"run {run_id} has not reached {from_node} yet")
        return {"kind": "rerun", "from_node": from_node, "intent": intent}

    def _fork_point(self, run_id: str, from_node: str) -> Any:
        """The latest checkpoint at which `from_node` was about to start."""
        return next((s for s in self.graph.get_state_history(self._thread(run_id)) if s.next == (from_node,)), None)

    # Advancing: run the pipeline forward until it pauses or finishes.

    def advance(self, run_id: str, step: Step) -> None:
        kind = step["kind"]
        with telemetry.span("run.advance", demogen__run_id=run_id, demogen__step=kind, demogen__from_stage=step.get("from_node")) as span:
            if kind == "start":
                self._invoke(run_id, step["state"])
            elif kind == "approve":
                self._invoke(run_id, Command(resume=step["decision"]))
            elif kind == "resume":
                self._invoke(run_id, None)
            elif kind == "rerun":
                self._rerun(run_id, step["from_node"], step.get("intent"))
            else:
                raise RunError(f"unknown step: {kind}")
            span.set_attribute("demogen.phase", self.status(run_id).phase)

    def _invoke(self, run_id: str, graph_input: Any, config: dict[str, Any] | None = None) -> None:
        try:
            self.graph.invoke(graph_input, config or self._thread(run_id))
        except DemogenError as error:
            raise RunError(f"{error}\nThe run is saved. Fix the cause, then resume it.") from error

    def _rerun(self, run_id: str, from_node: str, intent: str | None) -> None:
        """Fork the run at the point where `from_node` was about to start, and run forward again."""
        ctx = self.context(run_id)
        config = self._fork_point(run_id, from_node).config
        if intent is not None:
            playbook = parse_playbook(ctx.store.get_bytes("playbook.yaml").decode())
            update = {
                "intent_src": ctx.store.put_bytes("intent.md", intent.encode()),
                "intent": ctx.store.put_json("intent.json", merge_intent(playbook, intent).model_dump(mode="json")),
            }
            config = self.graph.update_state(config, update)
        if from_node in WIRING:
            # The stage's cached result would otherwise answer straight away with the old output.
            StageCache(ctx.store).invalidate(from_node)
        self._invoke(run_id, None, config)

    # Doing both at once, for callers that wait: the command line and tests.

    def start(
        self,
        playbook: Path | None,
        intent: Path | None,
        config: Config,
        run_id: str | None = None,
        *,
        prompt: str | None = None,
        url: str | None = None,
    ) -> RunStatus:
        run_id, step = self.prepare_start(
            playbook.read_bytes() if playbook else None,
            intent.read_bytes() if intent else None,
            config,
            run_id,
            playbook.stem if playbook else "demo",
            prompt=prompt,
            url=url,
        )
        self.advance(run_id, step)
        return self.status(run_id)

    def approve(self, run_id: str, edit: Path | None = None, feedback: str | None = None) -> RunStatus:
        self.advance(run_id, self.prepare_approval(run_id, edit.read_text() if edit else None, feedback))
        return self.status(run_id)

    def resume(self, run_id: str) -> RunStatus:
        """Continue an incomplete run (after a crash or a failed stage) from its last checkpoint."""
        step = self.prepare_resume(run_id)
        if step:
            self.advance(run_id, step)
        return self.status(run_id)

    def rerun(self, run_id: str, from_node: str, intent: Path | None = None) -> RunStatus:
        self.advance(run_id, self.prepare_rerun(run_id, from_node, intent.read_text() if intent else None))
        return self.status(run_id)

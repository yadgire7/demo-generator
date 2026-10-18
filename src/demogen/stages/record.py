"""The browser stages: sign in, rehearse the playbook into a plan, and record the take."""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from demogen import media
from demogen.driver.lockfile import Plan, ResolvedStep
from demogen.driver.playwright.rehearsal import StepUnresolved, resolve_step
from demogen.driver.playwright.session import ActionFailed, BrowserSession
from demogen.driver.playwright.take import StepFailed, run_take
from demogen.failures import FailureLog, model_name
from demogen.llm.base import llm_for
from demogen.playbook.schema import Playbook
from demogen.playbook.secrets import Redactor
from demogen.stages.base import StageContext, StageError, stage

SEED_TIMEOUT_S = 300


def _playbook(ctx: StageContext, inputs: dict[str, Any]) -> Playbook:
    return Playbook.model_validate(ctx.store.get_json(inputs["playbook"]))


def _session(ctx: StageContext, playbook: Playbook, inputs: dict[str, Any]) -> BrowserSession:
    auth = ctx.store.get_json(inputs["auth_state"]) if inputs.get("auth_state") else None
    return BrowserSession(playbook.target, headless=ctx.config.recording.headless, storage_state=auth)


def _redactor(playbook: Playbook) -> Redactor:
    return Redactor.for_text(playbook.model_dump_json())


def _failures(ctx: StageContext, playbook: Playbook) -> FailureLog:
    return FailureLog(ctx.store, ctx.run_id, playbook, _redactor(playbook), model_name(ctx.config))


def run_seed(playbook: Playbook) -> None:
    """Put the app into its known starting state. Runs before every stage that drives it."""
    if not playbook.setup.seed:
        return
    try:
        subprocess.run(playbook.setup.seed, shell=True, check=True, capture_output=True, text=True, timeout=SEED_TIMEOUT_S)
    except subprocess.CalledProcessError as error:
        raise StageError(f"the seed command failed ({error.returncode}): {error.stderr.strip()[-500:]}") from error
    except subprocess.TimeoutExpired as error:
        raise StageError(f"the seed command did not finish within {SEED_TIMEOUT_S}s") from error


@stage("setup", pool="browser", config_keys=("models.planner", "recording.headless"))
def setup(ctx: StageContext, inputs: dict[str, Any]) -> dict[str, Any]:
    """Carry out the playbook's unrecorded setup steps (signing in) and keep the browser state."""
    playbook = _playbook(ctx, inputs)
    if not playbook.setup.steps:
        return {"auth_state": None}
    run_seed(playbook)
    llm = llm_for(ctx.config, "planner")
    failures = _failures(ctx, playbook)
    with _session(ctx, playbook, {}) as session:
        session.open()
        for number, step in enumerate(playbook.setup.steps, start=1):
            try:
                resolve_step(session, llm, _redactor(playbook), step_id=f"setup-{number}", do=step.do, journal=failures.journal("setup", f"setup-{number}"))
            except StepUnresolved as error:
                raise StageError(f"setup step {number} could not be done: {error}") from error
        state = session.context.storage_state()
    profile = playbook.target.auth.profile if playbook.target.auth else "state"
    return {"auth_state": ctx.store.put_json(f"auth/{profile}.json", state)}


# Version 2: CSS paths skip generated ids, and unnamed elements are questioned before use.
@stage("rehearse", version=2, pool="browser", config_keys=("models.planner", "recording.headless"))
def rehearse(ctx: StageContext, inputs: dict[str, Any]) -> dict[str, Any]:
    """Walk the playbook once in a live browser, turning each step into replayable actions."""
    playbook = _playbook(ctx, inputs)
    run_seed(playbook)
    steps: list[ResolvedStep] = []
    failures = _failures(ctx, playbook)
    with _session(ctx, playbook, inputs) as session:
        session.open()
        for step in playbook.steps:
            if step.action:
                try:
                    for action in step.action:
                        session.perform(action)
                except ActionFailed as error:
                    failures.record(session, phase="rehearsal", kind=error.kind, step_id=step.id, message=str(error), action=action, step_outcome="failed")
                    raise StageError(f"step {step.id!r}: the action pinned in the playbook failed: {error}") from error
                steps.append(ResolvedStep(step_id=step.id, source="playbook", actions=step.action))
                continue
            try:
                resolved = resolve_step(
                    session,
                    llm_for(ctx.config, "planner"),
                    _redactor(playbook),
                    step_id=step.id,
                    do=step.do,
                    expect=step.expect,
                    data=step.data,
                    done=[earlier.do for earlier in playbook.steps[: len(steps)]],
                    journal=failures.journal("rehearsal", step.id, {"rehearsal": (inputs.get("rehearsal_attempt") or 0) + 1}),
                )
            except StepUnresolved as error:
                # Not a fault to stop on: the playbook asks for something this page cannot do.
                # The run goes back to the playbook review with the reason, for the author to revise.
                return {"plan": None, "rehearsal_failure": {"step_id": step.id, "reason": str(error)}}
            steps.append(resolved)
    return {"plan": ctx.store.put_json("actions.lock.json", Plan(steps=steps).model_dump(mode="json")), "rehearsal_failure": None}


def _heal(ctx: StageContext, playbook: Playbook, plan: Plan, failure: StepFailed, inputs: dict[str, Any], failures: FailureLog, take: int) -> Plan:
    """Re-resolve the step that broke, from the page as the earlier steps leave it."""
    broken = plan.step(failure.step_id)
    what_broke = f"step {broken.step_id!r} failed in the take ({failure.reason})"
    if broken.source == "playbook":
        raise StageError(f"{what_broke}; its actions are pinned in the playbook, so it is not repaired")
    step = next(s for s in playbook.steps if s.id == broken.step_id)
    index = plan.steps.index(broken)
    attempt = {"take": take, "repairing": broken.step_id}
    run_seed(playbook)
    with _session(ctx, playbook, inputs) as session:
        session.open()
        # Back to where the broken step begins, by the plan's own actions.
        for earlier in plan.steps[:index]:
            for action in earlier.actions:
                try:
                    session.perform(action)
                except ActionFailed as error:
                    failures.record(session, phase="repair", kind=error.kind, step_id=earlier.step_id, message=str(error), action=action, attempt=attempt, step_outcome="failed")
                    raise StageError(
                        f"{what_broke}, and it could not be repaired: replaying step {earlier.step_id!r} "
                        f"to get back to it, {error}. That step worked minutes earlier, so the site may be "
                        "showing a different page to this browser: see the screenshot in the run's failures folder."
                    ) from error
        try:
            repaired = resolve_step(
                session,
                llm_for(ctx.config, "planner"),
                _redactor(playbook),
                step_id=step.id,
                do=step.do,
                expect=step.expect,
                data=step.data,
                done=[s.do for s in playbook.steps[:index]],
                journal=failures.journal("repair", step.id, attempt),
            )
        except StepUnresolved as error:
            raise StageError(f"{what_broke}, and the model could not redo it: {error}") from error
    return Plan(steps=[*plan.steps[:index], repaired, *plan.steps[index + 1 :]])


@stage("record_take", pool="browser", config_keys=("models.planner", "recording"))
def record_take(ctx: StageContext, inputs: dict[str, Any]) -> dict[str, Any]:
    """Replay the plan on camera. A step that breaks is repaired and the take starts over."""
    playbook = _playbook(ctx, inputs)
    plan = Plan.model_validate(ctx.store.get_json(inputs["plan"]))
    recording = ctx.config.recording
    failures = _failures(ctx, playbook)
    with tempfile.TemporaryDirectory() as scratch:
        video = Path(scratch) / "raw.mp4"
        for take in range(1, recording.max_heals + 2):
            run_seed(playbook)
            try:
                with _session(ctx, playbook, inputs) as session:

                    def broke(step_id: str, action: Any, error: ActionFailed, take: int = take, session: Any = session) -> None:
                        failures.record(session, phase="take", kind=error.kind, step_id=step_id, message=str(error), action=action, attempt={"take": take}, step_outcome="failed")

                    events = run_take(session, playbook, plan, video, fps=recording.fps, crf=recording.crf, on_failure=broke)
                break
            except StepFailed as failure:
                if take > recording.max_heals:
                    raise StageError(f"the take kept failing: {failure}") from failure
                plan = _heal(ctx, playbook, plan, failure, inputs, failures, take)
        return {
            "events": ctx.store.put_bytes("events.jsonl", events.dump()),
            "video": ctx.store.put_file("raw.mp4", video),
            # The plan as recorded: identical to the approved one unless a step had to be repaired.
            "plan": ctx.store.put_json("actions.lock.json", plan.model_dump(mode="json")),
        }


@stage("verify_take")
def verify_take(ctx: StageContext, inputs: dict[str, Any]) -> dict[str, Any]:
    """Every playbook step must have a start and a later end, inside the recorded video."""
    playbook = _playbook(ctx, inputs)
    starts: dict[str, float] = {}
    ends: dict[str, float] = {}
    for line in ctx.store.get_bytes(inputs["events"]["path"]).decode().splitlines():
        event = json.loads(line)
        if event["type"] == "step_start":
            starts[event["step_id"]] = event["t"]
        elif event["type"] == "step_end":
            ends[event["step_id"]] = event["t"]
    windows = {}
    for step in playbook.steps:
        start, end = starts.get(step.id), ends.get(step.id)
        if start is None or end is None or end <= start:
            raise StageError(f"take is incomplete: step {step.id!r} has no valid window")
        windows[step.id] = {"start": start, "end": end}
    if inputs.get("video"):
        seconds = media.probe(ctx.store.local_path(inputs["video"])).seconds
        if seconds < max(ends.values()) - 0.25:
            raise StageError(f"the video ({seconds:.1f}s) ends before the last step does ({max(ends.values()):.1f}s)")
    return {"windows": ctx.store.put_json("windows.json", windows)}

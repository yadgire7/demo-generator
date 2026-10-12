"""The pipeline as a LangGraph graph.

`demogen.graph` is the only package that imports LangGraph. Nodes are thin: they hand named
state keys to a stage through the executor and write the stage's outputs back.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send, interrupt

from demogen.exec.base import StageExecutor
from demogen.stages import StageContext
from demogen.store.artifacts import ArtifactRef

STATE_VERSION = 1


def _merge(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    return {**left, **right}


class RunState(TypedDict, total=False):
    """Refs and small scalars only. Artifact contents never enter the checkpoint."""

    state_version: int
    run_id: str
    playbook_src: ArtifactRef
    intent_src: ArtifactRef | None
    playbook: ArtifactRef
    intent: ArtifactRef
    auth_state: ArtifactRef | None
    plan: ArtifactRef
    events: ArtifactRef
    video: ArtifactRef | None
    windows: ArtifactRef
    keyframes: ArtifactRef
    facts: ArtifactRef
    story: ArtifactRef
    clips: Annotated[dict[str, dict[str, Any]], _merge]
    words: ArtifactRef
    edl: ArtifactRef
    flagged: list[str]
    rewrite_round: int
    final: dict[str, Any]
    report: ArtifactRef


# node -> (state keys passed to the stage of the same name, stage outputs written back to state)
WIRING: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "load_validate": (("playbook_src", "intent_src"), ("playbook", "intent")),
    "setup": (("playbook",), ("auth_state",)),
    "rehearse": (("playbook", "auth_state"), ("plan",)),
    "record_take": (("playbook", "plan", "auth_state"), ("events", "video", "plan")),
    "verify_take": (("playbook", "events"), ("windows",)),
    "extract_keyframes": (("video", "events", "windows"), ("keyframes",)),
    "analyze_frames": (("playbook", "keyframes"), ("facts",)),
    # `rewrite_round` keeps each rewrite attempt a distinct cache entry: a retry must not be
    # answered with the result of the attempt it is retrying.
    # On a rewrite, `story` and `clips` are the narration as it stood and how long each passage ran.
    "write_story": (
        ("playbook", "intent", "facts", "windows", "flagged", "rewrite_round", "story", "clips"),
        ("story",),
    ),
    "qa_audio": (("story", "clips"), ("words",)),
    "plan_timeline": (("windows", "clips"), ("edl", "flagged")),
    "render": (("video", "events", "edl", "words"), ("final",)),
    "final_qa": (("final", "edl", "flagged", "words"), ("report",)),
}

# Approval pauses: node -> (what is being approved, the state key holding it)
APPROVALS = {"approve_plan": ("plan", "plan"), "approve_story": ("story", "story")}

# Nodes a rerun may start from. `synthesize_step` is excluded: it only runs as a fan-out task.
RERUNNABLE = (*WIRING, *APPROVALS)


def build_graph(executor: StageExecutor, ctx_for: Callable[[str], StageContext]) -> StateGraph:
    def stage_node(name: str) -> Callable[[RunState], dict[str, Any]]:
        reads, writes = WIRING[name]

        def node(state: RunState) -> dict[str, Any]:
            outputs = executor.run(name, ctx_for(state["run_id"]), {key: state.get(key) for key in reads})
            return {key: outputs[key] for key in writes}

        return node

    def approval_node(name: str) -> Callable[[RunState], dict[str, Any]]:
        kind, key = APPROVALS[name]

        def node(state: RunState) -> dict[str, Any]:
            if ctx_for(state["run_id"]).config.auto_approve:
                return {}
            # On resume this node runs again from the top, so nothing may precede the interrupt.
            decision = interrupt({"kind": kind, "artifact": state[key]})
            return {key: decision["artifact"]} if decision.get("artifact") else {}

        return node

    def write_story(state: RunState) -> dict[str, Any]:
        update = stage_node("write_story")(state)
        if state.get("flagged"):
            update["rewrite_round"] = state.get("rewrite_round", 0) + 1
        return update

    def fan_out_synthesis(state: RunState) -> list[Send]:
        story = ctx_for(state["run_id"]).store.get_json(state["story"])
        return [
            Send("synthesize_step", {"run_id": state["run_id"], "step_id": step["step_id"], "text": step["text"]})
            for step in story["steps"]
        ]

    def synthesize_step(task: dict[str, Any]) -> dict[str, Any]:
        inputs = {"step_id": task["step_id"], "text": task["text"]}
        clip = executor.run("synthesize_step", ctx_for(task["run_id"]), inputs)
        return {"clips": {task["step_id"]: clip}}

    def after_timeline(state: RunState) -> str:
        rounds_left = state.get("rewrite_round", 0) < ctx_for(state["run_id"]).config.max_rewrite_rounds
        return "write_story" if state.get("flagged") and rounds_left else "render"

    graph = StateGraph(RunState)
    for name in WIRING:
        graph.add_node(name, write_story if name == "write_story" else stage_node(name))
    for name in APPROVALS:
        graph.add_node(name, approval_node(name))
    graph.add_node("synthesize_step", synthesize_step)

    spine = [
        START, "load_validate", "setup", "rehearse", "approve_plan", "record_take", "verify_take",
        "extract_keyframes", "analyze_frames", "write_story", "approve_story",
    ]  # fmt: skip
    for source, target in zip(spine, spine[1:]):
        graph.add_edge(source, target)
    graph.add_conditional_edges("approve_story", fan_out_synthesis, ["synthesize_step"])
    graph.add_edge("synthesize_step", "qa_audio")
    graph.add_edge("qa_audio", "plan_timeline")
    graph.add_conditional_edges("plan_timeline", after_timeline, ["write_story", "render"])
    graph.add_edge("render", "final_qa")
    graph.add_edge("final_qa", END)
    return graph

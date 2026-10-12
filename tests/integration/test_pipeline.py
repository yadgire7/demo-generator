"""The control plane end to end, over the placeholder stages: pause, approve, resume, loop, rerun."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from demogen.graph.runtime import RunError
from demogen.stages import REGISTRY, StageError
from tests.conftest import fake_qa_audio

STEPS = ["add-first", "add-second", "complete-first", "filter-active"]
ONCE_PER_RUN = [name for name in REGISTRY if name != "synthesize_step"]


def test_auto_approved_run_completes(harness, playbook):
    h = harness()
    status = h.start(playbook, auto_approve=True)
    assert status.phase == "done"
    assert all(h.calls[name] == 1 for name in ONCE_PER_RUN), h.calls
    assert h.calls["synthesize_step"] == len(STEPS)
    assert set(status.state["clips"]) == set(STEPS)
    report = h.json("r1", "report.json")
    assert report["steps"] == 4 and "render" in report["stub_stages"]
    assert [s["step_id"] for s in h.json("r1", "edl.json")["steps"]] == STEPS


def test_run_pauses_for_plan_then_story(harness, playbook):
    h = harness()
    status = h.start(playbook)
    assert status.phase == "awaiting_approval" and status.awaiting["kind"] == "plan"
    assert h.calls["record_take"] == 0  # nothing is recorded before the plan is approved

    status = h.runtime.approve("r1")
    assert status.phase == "awaiting_approval" and status.awaiting["kind"] == "story"
    assert h.calls["record_take"] == 1 and h.calls["synthesize_step"] == 0

    assert h.runtime.approve("r1").phase == "done"
    with pytest.raises(RunError, match="not waiting"):
        h.runtime.approve("r1")


def test_a_pause_survives_a_new_process(harness, playbook):
    first = harness()
    first.start(playbook)
    first.runtime.close()
    second = harness()  # same runs directory, fresh runtime: as if the CLI had exited
    assert second.runtime.status("r1").awaiting["kind"] == "plan"
    second.runtime.approve("r1")
    assert second.runtime.approve("r1").phase == "done"
    assert second.calls["rehearse"] == 0  # the plan came from the checkpoint, not a second rehearsal


def test_edited_story_is_what_gets_voiced(harness, playbook, tmp_path):
    h = harness()
    h.start(playbook)
    h.runtime.approve("r1")
    story = h.json("r1", "story.json")
    story["steps"][1]["text"] = "A completely different line."
    edit = tmp_path / "story.yaml"
    edit.write_text(yaml.safe_dump(story))

    assert h.runtime.approve("r1", edit).phase == "done"
    words = h.json("r1", "words.json")["steps"]
    assert [w["w"] for w in words["add-second"]] == ["A", "completely", "different", "line."]
    assert [w["w"] for w in words["add-first"]] == story["steps"][0]["text"].split()


def test_edited_story_may_not_leave_a_passage_blank(harness, playbook, tmp_path):
    h = harness()
    h.start(playbook)
    h.runtime.approve("r1")
    story = h.json("r1", "story.json")
    story["steps"][0]["text"] = "  "
    edit = tmp_path / "story.yaml"
    edit.write_text(yaml.safe_dump(story))
    with pytest.raises(RunError, match="needs some words to say"):
        h.runtime.approve("r1", edit)


def test_edit_must_cover_the_playbook_steps(harness, playbook, tmp_path):
    h = harness()
    h.start(playbook)
    plan = h.json("r1", "actions.lock.json")
    plan["steps"].pop()
    edit = tmp_path / "plan.json"
    edit.write_text(yaml.safe_dump(plan))
    with pytest.raises(RunError, match="exactly the playbook's steps"):
        h.runtime.approve("r1", edit)
    assert h.runtime.status("r1").awaiting["kind"] == "plan"  # still waiting, nothing consumed


def test_failed_stage_resumes_without_repeating_earlier_work(harness, playbook):
    failures = iter([StageError("speech-to-text worker died")])

    def flaky_qa(ctx, inputs):
        for failure in failures:
            raise failure
        return fake_qa_audio(ctx, inputs)

    h = harness({"qa_audio": flaky_qa})
    with pytest.raises(RunError, match="demogen resume r1"):
        h.start(playbook, auto_approve=True)
    status = h.runtime.status("r1")
    assert status.phase == "incomplete" and status.next == ("qa_audio",)

    assert h.runtime.resume("r1").phase == "done"
    assert h.calls["qa_audio"] == 2
    assert h.calls["rehearse"] == h.calls["record_take"] == h.calls["write_story"] == 1
    assert h.calls["synthesize_step"] == len(STEPS)


LONG_HINT = "This single step has a narration hint that runs far longer than three seconds of footage can carry"


def _long_playbook(playbook: Path, tmp_path: Path) -> Path:
    data = yaml.safe_load(playbook.read_text())
    data["steps"][1]["say_hint"] = LONG_HINT
    path = tmp_path / "long.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


def test_overrunning_narration_is_rewritten(harness, playbook, tmp_path):
    h = harness()
    status = h.start(_long_playbook(playbook, tmp_path), auto_approve=True, target_wps=2.4)
    assert status.phase == "done"
    assert status.state["rewrite_round"] == 1 and status.state["flagged"] == []
    assert h.calls["write_story"] == 2 and h.calls["record_take"] == 1
    # Only the rewritten step is voiced again; the other three clips come from the cache.
    assert h.calls["synthesize_step"] == len(STEPS) + 1
    rewritten = h.json("r1", "story.json")["steps"][1]["text"]
    assert rewritten == " ".join(LONG_HINT.split()[:7])  # 3.0 s x 2.4 words/s


def test_rewrites_are_bounded(harness, playbook, tmp_path):
    def never_fits(ctx, inputs):
        return {**REGISTRY["plan_timeline"].fn(ctx, inputs), "flagged": ["add-second"]}

    h = harness({"plan_timeline": never_fits})
    status = h.start(playbook, auto_approve=True, max_rewrite_rounds=2)
    assert status.phase == "done"
    assert status.state["rewrite_round"] == 2 and h.calls["write_story"] == 3
    assert h.json("r1", "report.json")["overrunning_steps"] == ["add-second"]


def test_rerun_from_story_with_new_intent_keeps_the_take(harness, playbook, tmp_path):
    h = harness()
    h.start(playbook, auto_approve=True)
    events_before = h.runtime.status("r1").state["events"]
    brief = tmp_path / "intent.md"
    brief.write_text("Lead with how fast it is.")

    status = h.runtime.rerun("r1", "write_story", brief)
    assert status.phase == "done"
    assert h.json("r1", "intent.json")["notes"] == "Lead with how fast it is."
    assert h.calls["write_story"] == 2
    assert h.calls["rehearse"] == h.calls["record_take"] == h.calls["verify_take"] == 1
    assert status.state["events"] == events_before


def test_rerun_rejects_unknown_or_unreached_stages(harness, playbook):
    h = harness()
    h.start(playbook)  # paused at plan approval
    with pytest.raises(RunError, match="cannot rerun from"):
        h.runtime.rerun("r1", "synthesize_step")
    with pytest.raises(RunError, match="has not reached"):
        h.runtime.rerun("r1", "render")


def test_unknown_run_and_duplicate_run(harness, playbook):
    h = harness()
    with pytest.raises(RunError, match="no run named"):
        h.runtime.status("nope")
    h.start(playbook, auto_approve=True)
    with pytest.raises(RunError, match="already exists"):
        h.start(playbook, auto_approve=True)


def test_langgraph_is_imported_only_by_the_graph_package():
    src = Path(__file__).parents[2] / "src" / "demogen"
    offenders = [
        str(path.relative_to(src))
        for path in src.rglob("*.py")
        if "graph" not in path.relative_to(src).parts
        and re.search(r"^\s*(from|import)\s+(langgraph|langchain)", path.read_text(), re.M)
    ]
    assert offenders == []

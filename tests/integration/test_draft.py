"""Starting a run from a prompt: the playbook is drafted, and the author approves it, edits it or sends it back."""

from __future__ import annotations

import pytest
import yaml
from pydantic import ValidationError
from typer.testing import CliRunner

from demogen.cli import app
from demogen.graph.runtime import RunError
from demogen.llm.base import LLMError
from demogen.playbook.draft import draft_playbook
from demogen.playbook.loader import parse_playbook
from tests.conftest import SCRIPTED, ScriptedLLM

PROMPT = "Show how to add a todo and tick it off in TodoMVC, for developers who are short on time."
URL = "https://demo.playwright.dev/todomvc"


def _draft(*steps, name="Todos in a hurry", cta=""):
    return {
        "name": name, "base_url": URL, "audience": "developers short on time", "tone": "brisk", "key_messages": ["It takes seconds"], "cta": cta,
        "steps": [{"id": step_id, "do": do, "expect": f"{do} has worked", "say_hint": "It is quick.", "dwell_seconds": 2} for step_id, do in steps],
    }  # fmt: skip


FIRST = _draft(("add", "Add a todo called Ship it"), ("tick", "Mark Ship it as done"))
SECOND = _draft(("add", "Add a todo called Ship it"), ("tick", "Mark Ship it as done"), ("filter", "Show only the todos still to do"))


def test_draft_is_a_valid_readable_playbook():
    llm = ScriptedLLM(replies=[FIRST])
    text = draft_playbook(llm, PROMPT, URL)
    playbook = parse_playbook(text)
    assert [(s.id, s.do, s.dwell) for s in playbook.steps] == [("add", "Add a todo called Ship it", 2.0), ("tick", "Mark Ship it as done", 2.0)]
    assert playbook.target.base_url == URL and playbook.intent.audience == "developers short on time" and playbook.intent.cta is None
    assert text.index("name:") < text.index("target:") < text.index("intent:") < text.index("steps:")  # in the order an author writes it
    request = llm.transcript[1]
    assert PROMPT in request and f"The demo starts at: {URL}" in request and request.endswith("Write the playbook.")


def test_draft_that_breaks_the_playbooks_rules_is_sent_back_to_the_model():
    duplicate = _draft(("add", "Add a todo"), ("add", "Add another"))
    llm = ScriptedLLM(replies=[duplicate, _draft(("Add One", "Add a todo")), FIRST])
    assert len(parse_playbook(draft_playbook(llm, PROMPT)).steps) == 2
    assert "duplicate step id: add" in llm.requests[1][-1]["content"]
    assert "steps.0.id" in llm.requests[2][-1]["content"]  # ids must be lowercase slugs
    with pytest.raises(LLMError, match="base_url must be a full address"):
        draft_playbook(ScriptedLLM(replies=[{**FIRST, "base_url": "todomvc"}] * 3), PROMPT)


def test_redraft_shows_the_model_its_draft_and_the_authors_feedback():
    llm = ScriptedLLM(replies=[SECOND])
    draft_playbook(llm, PROMPT, None, previous="version: 1\nname: Todos in a hurry\n", feedback="Add a step that filters to what is left.")
    request = llm.transcript[1]
    assert "Your current draft:\nversion: 1\nname: Todos in a hurry" in request
    assert "asks for these changes:\nAdd a step that filters to what is left." in request and "The demo starts at" not in request


def test_run_from_a_prompt_pauses_on_the_draft_then_carries_on(harness, scripted_llm):
    scripted_llm.replies = [FIRST]
    h = harness()
    status = h.start(None, prompt=PROMPT, url=URL, **SCRIPTED)
    assert status.phase == "awaiting_approval" and status.awaiting == {"kind": "playbook", "artifact": status.state["playbook_src"]}
    assert h.path("r1", "prompt.md").read_text() == PROMPT
    assert [s.id for s in parse_playbook(h.path("r1", "playbook.yaml").read_text()).steps] == ["add", "tick"]
    assert h.calls["load_validate"] == 0  # nothing downstream runs on an unapproved draft

    status = h.runtime.approve("r1")
    assert status.awaiting["kind"] == "plan"
    assert [s["step_id"] for s in h.json("r1", "actions.lock.json")["steps"]] == ["add", "tick"]
    h.runtime.approve("r1")
    assert h.runtime.approve("r1").phase == "done" and h.calls["draft_playbook"] == 1


def test_feedback_sends_the_draft_back_as_often_as_needed(harness, scripted_llm):
    third = _draft(("add", "Add a todo called Ship it"), ("filter", "Show only the todos still to do"))
    scripted_llm.replies = [FIRST, SECOND, third]
    h = harness()
    h.start(None, prompt=PROMPT, **SCRIPTED)

    status = h.runtime.approve("r1", feedback="Add a step that filters to what is left.")
    assert status.awaiting["kind"] == "playbook" and status.state["draft_round"] == 1 and status.state["playbook_feedback"] is None
    assert [s.id for s in parse_playbook(h.path("r1", "playbook.yaml").read_text()).steps] == ["add", "tick", "filter"]
    asked = scripted_llm.transcript[1]
    assert "Add a step that filters to what is left." in asked and "id: tick" in asked  # it revises its own first draft

    status = h.runtime.approve("r1", feedback="Drop the ticking step.")
    assert status.state["draft_round"] == 2 and "id: filter" in scripted_llm.transcript[1]
    assert h.calls["draft_playbook"] == 3 and h.calls["load_validate"] == 0

    assert h.runtime.approve("r1").awaiting["kind"] == "plan"
    assert [s["step_id"] for s in h.json("r1", "actions.lock.json")["steps"]] == ["add", "filter"]


def test_author_can_replace_the_draft_with_their_own_file(harness, scripted_llm, tmp_path):
    scripted_llm.replies = [FIRST]
    h = harness()
    h.start(None, prompt=PROMPT, **SCRIPTED)
    mine = yaml.safe_load(h.path("r1", "playbook.yaml").read_text())
    mine["steps"][1]["do"] = "Tick the todo off with one click"
    edit = tmp_path / "mine.yaml"
    edit.write_text("# my own notes survive\n" + yaml.safe_dump(mine, sort_keys=False))

    broken = tmp_path / "broken.yaml"
    broken.write_text(yaml.safe_dump({**mine, "steps": []}))
    with pytest.raises(ValidationError, match="steps"):
        h.runtime.approve("r1", broken)
    assert h.runtime.status("r1").awaiting["kind"] == "playbook"  # the bad file changed nothing

    assert h.runtime.approve("r1", edit).awaiting["kind"] == "plan"
    assert h.path("r1", "playbook.yaml").read_text().startswith("# my own notes survive")
    assert h.json("r1", "playbook.json")["steps"][1]["do"] == "Tick the todo off with one click"
    assert h.calls["draft_playbook"] == 1  # an edit is taken as it is, not redrafted


def test_feedback_is_only_for_the_drafted_playbook(harness, scripted_llm, playbook, tmp_path):
    scripted_llm.replies = [FIRST]
    h = harness()
    h.start(None, prompt=PROMPT, **SCRIPTED)
    edit = tmp_path / "mine.yaml"
    edit.write_text(h.path("r1", "playbook.yaml").read_text())
    with pytest.raises(RunError, match="either an edited file or feedback"):
        h.runtime.approve("r1", edit, feedback="Shorter.")
    h.runtime.approve("r1")
    with pytest.raises(RunError, match="only taken for a drafted playbook; to change the plan"):
        h.runtime.approve("r1", feedback="Click somewhere else.")


def test_start_needs_a_playbook_or_a_prompt_and_not_both(harness, playbook):
    h = harness()
    for arguments in (dict(playbook=None), dict(playbook=None, prompt="   "), dict(playbook=playbook, prompt=PROMPT)):
        with pytest.raises(RunError, match="either a playbook or a prompt"):
            h.start(arguments.pop("playbook"), **arguments)
    assert not (h.runs_dir / "r1").exists()


def test_auto_approve_and_rerun_cover_the_draft_too(harness, scripted_llm):
    scripted_llm.replies = [FIRST, SECOND]
    h = harness()
    assert h.start(None, prompt=PROMPT, auto_approve=True, **SCRIPTED).phase == "done"
    assert h.runtime.rerun("r1", "draft_playbook").phase == "done"  # a fresh draft, and everything after it again
    assert h.calls["draft_playbook"] == 2 and h.calls["record_take"] == 2
    assert len(h.json("r1", "playbook.json")["steps"]) == 3


def test_command_line_from_prompt_to_feedback_to_approval(scripted_llm, tmp_path):
    scripted_llm.replies = [FIRST, SECOND]
    config = tmp_path / "demogen.yaml"
    config.write_text(yaml.safe_dump(SCRIPTED))
    runner, runs = CliRunner(), ["--runs-dir", str(tmp_path / "runs")]

    started = runner.invoke(app, ["run", "--prompt", PROMPT, "--url", URL, "--run-id", "p1", "--config", str(config), *runs])
    assert started.exit_code == 0 and "approve the playbook" in started.output and '--feedback "what to change"' in started.output
    revised = runner.invoke(app, ["approve", "p1", "--feedback", "Add a step that filters to what is left.", *runs])
    assert revised.exit_code == 0 and "approve the playbook" in revised.output
    assert "id: filter" in (tmp_path / "runs" / "p1" / "playbook.yaml").read_text()

    neither = runner.invoke(app, ["run", "--run-id", "p2", *runs])
    assert neither.exit_code == 1 and "either a playbook or a prompt" in neither.output

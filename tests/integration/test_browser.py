"""The browser stages against the fixture app: rehearsal, the recorded take, repair and sign-in."""

from __future__ import annotations

import json
import subprocess

import pytest
import yaml

from demogen.driver.lockfile import Plan
from demogen.graph.runtime import RunError
from tests.conftest import SCRIPTED, Ref


def _playbook(tmp_path, app_url, steps, page="index.html", **extra):
    data = {
        "version": 1,
        "name": "Fixture demo",
        "target": {"base_url": f"{app_url}/{page}", "viewport": {"width": 800, "height": 600, "device_scale_factor": 1}},
        "steps": steps,
        **extra,
    }
    path = tmp_path / "playbook.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


def _events(h, run_id="r1"):
    return [json.loads(line) for line in h.path(run_id, "events.jsonl").read_text().splitlines()]


def _probe(path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height,r_frame_rate:format=duration", "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    )  # fmt: skip
    data = json.loads(out.stdout)
    return {**data["streams"][0], "duration": float(data["format"]["duration"])}


ADD_AND_FINISH = [
    {"id": "add-task", "do": "Add a task called Ship it", "expect": "The task is listed"},
    {"id": "finish-task", "do": "Mark Ship it as done", "expect": "Nothing is left", "dwell": "1s"},
]
ADD_AND_FINISH_SCRIPT = [
    [("fill", {"ref": Ref('textbox "New task"'), "text": "Ship it"}), ("click", {"ref": Ref('button "Add"')})],
    [("step_done", {"evidence": ["Ship it", "1 left"]})],
    [("click", {"ref": Ref('checkbox "Done: Ship it"')})],
    [("step_done", {"evidence": ["0 left"]})],
]


def test_rehearsal_turns_model_actions_into_a_durable_plan(harness, scripted_llm, app_url, tmp_path):
    scripted_llm.turns = list(ADD_AND_FINISH_SCRIPT)
    h = harness(real="browser")
    status = h.start(_playbook(tmp_path, app_url, ADD_AND_FINISH), **SCRIPTED)
    assert status.awaiting["kind"] == "plan"

    plan = Plan.model_validate(h.json("r1", "actions.lock.json"))
    add, finish = plan.steps
    assert [a.type for a in add.actions] == ["fill", "click"] and add.source == "llm"
    assert add.actions[0].text == "Ship it" and add.expect_text == ["Ship it", "1 left"]
    # Elements are stored by role and name first, with a CSS path to fall back on: never by ref.
    button = add.actions[1]
    assert (button.target[0].by, button.target[0].role, button.target[0].name) == ("role", "button", "Add")
    assert button.target[1].by == "css" and button.label == 'button "Add"'
    assert finish.actions[0].target[0].name == "Done: Ship it"
    assert "ref=" not in h.path("r1", "actions.lock.json").read_text()


def test_take_is_recorded_with_a_matching_event_log(harness, pinned_playbook):
    h = harness(real="browser")
    status = h.start(pinned_playbook, auto_approve=True)
    assert status.phase == "done"

    video = _probe(h.path("r1", "raw.mp4"))
    assert (video["width"], video["height"], video["r_frame_rate"]) == (800, 600, "30/1")

    events = _events(h)
    kinds = [e["type"] for e in events]
    assert kinds[0] == "meta" and kinds[-1] == "end"
    assert [e["t"] for e in events] == sorted(e["t"] for e in events)
    # The video runs for as long as the event log says the take did.
    assert abs(video["duration"] - events[-1]["t"]) < 0.25

    clicks = [e for e in events if e["type"] == "click"]
    assert [c["step_id"] for c in clicks] == ["add-task", "add-task", "finish-task"]
    # The pointer travels to each click: its last sample before the click is the click point.
    for click in clicks:
        last = [e for e in events if e["type"] == "cursor" and e["t"] <= click["t"]][-1]
        assert (last["x"], last["y"]) == (click["x"], click["y"])
    assert kinds.count("cursor") > 30

    windows = h.json("r1", "windows.json")
    # One step ends as the next begins (two clock readings, so they may differ by a millisecond).
    assert windows["add-task"]["end"] == pytest.approx(windows["finish-task"]["start"], abs=0.002)


def test_scale_factor_sets_the_video_resolution(harness, pinned_playbook):
    data = yaml.safe_load(pinned_playbook.read_text())
    data["target"]["viewport"]["device_scale_factor"] = 2
    pinned_playbook.write_text(yaml.safe_dump(data))
    h = harness(real="browser")
    h.start(pinned_playbook, auto_approve=True)
    video = _probe(h.path("r1", "raw.mp4"))
    assert (video["width"], video["height"]) == (1600, 1200)
    assert json.loads(h.path("r1", "events.jsonl").read_text().splitlines()[0])["viewport"] == [800, 600]


def test_offscreen_elements_are_scrolled_into_view(harness, app_url, tmp_path):
    steps = [{"id": "archive", "do": "Archive everything", "action": [{"click": {"css": "#archive"}}]}]
    h = harness(real="browser")
    assert h.start(_playbook(tmp_path, app_url, steps), auto_approve=True).phase == "done"
    click = next(e for e in _events(h) if e["type"] == "click")
    assert 0 < click["y"] < 600  # the button starts far below the fold


def test_broken_step_is_repaired_and_the_take_restarts(harness, scripted_llm, app_url, tmp_path):
    scripted_llm.turns = list(ADD_AND_FINISH_SCRIPT)
    h = harness(real="browser")
    h.start(_playbook(tmp_path, app_url, ADD_AND_FINISH), **SCRIPTED)

    # Approve a plan whose second step points at an element that does not exist.
    plan = h.json("r1", "actions.lock.json")
    plan["steps"][1]["actions"][0]["target"] = [{"by": "role", "role": "checkbox", "name": "No such box"}]
    edit = tmp_path / "plan.yaml"
    edit.write_text(yaml.safe_dump(plan))
    scripted_llm.turns = [[("click", {"ref": Ref('checkbox "Done: Ship it"')})], [("step_done", {"evidence": ["0 left"]})]]

    status = h.runtime.approve("r1", edit)
    assert status.awaiting["kind"] == "story"
    repaired = Plan.model_validate(h.json("r1", "actions.lock.json")).steps[1]
    assert repaired.actions[0].target[0].name == "Done: Ship it"
    assert h.runtime.context("r1").store.verify(status.state["plan"])  # the run now points at the repaired plan
    # One clean take made it to disk: both steps, each clicked once.
    assert [e["step_id"] for e in _events(h) if e["type"] == "click"] == ["add-task", "add-task", "finish-task"]


def test_pinned_action_that_fails_is_reported_not_replaced(harness, app_url, tmp_path):
    steps = [{"id": "missing", "do": "Click a button that is not there", "action": [{"click": {"role": "button", "name": "Nope"}}]}]
    h = harness(real="browser")
    with pytest.raises(RunError, match="pinned in the playbook failed"):
        h.start(_playbook(tmp_path, app_url, steps), auto_approve=True)


def test_model_can_give_up_on_a_step(harness, scripted_llm, app_url, tmp_path):
    scripted_llm.turns = [[("step_failed", {"reason": "there is no export button"})]]
    h = harness(real="browser")
    steps = [{"id": "export", "do": "Export the tasks to CSV"}]
    with pytest.raises(RunError, match="there is no export button"):
        h.start(_playbook(tmp_path, app_url, steps), **SCRIPTED)


def test_evidence_must_really_be_on_the_page(harness, scripted_llm, app_url, tmp_path):
    scripted_llm.turns = [
        [("fill", {"ref": Ref('textbox "New task"'), "text": "Ship it"}), ("click", {"ref": Ref('button "Add"')})],
        [("step_done", {"evidence": ["Task saved successfully"]})],  # not on the page: rejected
        [("step_done", {"evidence": ["1 left"]})],
    ]
    h = harness(real="browser")
    h.start(_playbook(tmp_path, app_url, ADD_AND_FINISH[:1]), **SCRIPTED)
    assert "not visible on the page as written" in "\n".join(scripted_llm.transcript)
    assert h.json("r1", "actions.lock.json")["steps"][0]["expect_text"] == ["1 left"]


def test_sign_in_is_kept_out_of_the_recording_and_secrets_out_of_sight(harness, scripted_llm, app_url, tmp_path, monkeypatch):
    monkeypatch.setenv("DEMO_USER", "ada.lovelace")
    monkeypatch.setenv("DEMO_PASS", "correct-horse")
    scripted_llm.turns = [
        [
            ("fill", {"ref": Ref('textbox "Username"'), "text": "${secret:DEMO_USER}"}),
            ("fill", {"ref": Ref('textbox "Password"'), "text": "${secret:DEMO_PASS}"}),
            ("click", {"ref": Ref('button "Sign in"')}),
        ],
        [("step_done", {"evidence": ["Create report"]})],
        [("click", {"ref": Ref('button "Create report"')})],
        [("step_done", {"evidence": ["Report created"]})],
    ]
    setup = {"steps": [{"do": "Sign in as ${secret:DEMO_USER} with password ${secret:DEMO_PASS}"}]}
    steps = [{"id": "create", "do": "Create a report", "expect": "The report exists"}]
    h = harness(real="browser")
    playbook = _playbook(tmp_path, app_url, steps, page="private.html", setup=setup)
    assert h.start(playbook, auto_approve=True, **SCRIPTED).phase == "done"

    # The take opened the protected page already signed in, so it shows only the playbook's step.
    assert [e["step_id"] for e in _events(h) if e["type"] == "click"] == ["create"]
    # The model saw the signed-in page with the user's name blanked out, and never a secret.
    shown = "\n".join(scripted_llm.transcript)
    assert "Welcome, [REDACTED]" in shown
    assert "ada.lovelace" not in shown and "correct-horse" not in shown
    for artifact in ("actions.lock.json", "events.jsonl", "playbook.json"):
        text = h.path("r1", artifact).read_text()
        assert "ada.lovelace" not in text and "correct-horse" not in text


def test_typing_in_the_take_leaves_what_rehearsal_left(harness, scripted_llm, app_url, tmp_path):
    # The field refills itself with 1 when emptied. Rehearsal sets it to 3; the take, typing
    # like a person, must arrive at 3 too, not 13.
    scripted_llm.turns = [[("fill", {"ref": Ref('spinbutton "Copies"'), "text": "3"})], [("step_done", {"evidence": ["Copies to print: 3"]})]]
    steps = [{"id": "copies", "do": "Set the number of copies to 3", "expect": "Three copies are set"}]
    h = harness(real="browser")
    assert h.start(_playbook(tmp_path, app_url, steps), auto_approve=True, **SCRIPTED).phase == "done"
    assert h.calls["rehearse"] == 1 and h.json("r1", "actions.lock.json")["steps"][0]["expect_text"] == ["Copies to print: 3"]


def test_a_take_that_ends_up_somewhere_else_says_what_was_missing(harness, scripted_llm, app_url, tmp_path):
    scripted_llm.turns = [[("fill", {"ref": Ref('spinbutton "Copies"'), "text": "3"})], [("step_done", {"evidence": ["Copies to print: 3"]})]]
    steps = [{"id": "copies", "do": "Set the number of copies to 3"}]
    h = harness(real="browser")
    h.start(_playbook(tmp_path, app_url, steps), recording={"max_heals": 0}, **SCRIPTED)
    plan = h.json("r1", "actions.lock.json")
    plan["steps"][0]["expect_text"] = ["Copies to print: 30"]  # something the take will not produce
    edit = tmp_path / "plan.yaml"
    edit.write_text(yaml.safe_dump(plan))
    with pytest.raises(RunError, match="did not show 'Copies to print: 30', which it showed at this point in rehearsal"):
        h.runtime.approve("r1", edit)


def test_only_actions_that_do_something_are_recorded(harness, scripted_llm, app_url, tmp_path):
    scripted_llm.turns = [
        [("fill", {"ref": Ref('textbox "New task"'), "text": "Ship it"}), ("click", {"ref": Ref('button "Add"')})],
        [("wait", {"seconds": 1})],  # to look again: never recorded
        [("hover", {"ref": Ref('heading "Tasks"')})],  # changes nothing: left out
        [("goto", {"url": "index.html"})],  # already there: left out
        [("hover", {"ref": Ref('button "Help"')})],  # shows a tooltip at once: kept
        [("hover", {"ref": Ref('button "More"')})],  # opens a menu after a moment: kept
        [("step_done", {"evidence": ["Ship it"]})],
    ]
    steps = [{"id": "add", "do": "Add a task called Ship it"}]
    h = harness(real="browser")
    h.start(_playbook(tmp_path, app_url, steps), **SCRIPTED)
    actions = h.json("r1", "actions.lock.json")["steps"][0]["actions"]
    assert [(a["type"], a["label"]) for a in actions] == [
        ("fill", 'textbox "New task"'), ("click", 'button "Add"'), ("hover", 'button "Help"'), ("hover", 'button "More"'),
    ]  # fmt: skip
    told = "\n".join(scripted_llm.transcript)
    assert "Hovering there changed nothing on the page, so it is not part of the recording." in told
    assert "The page is already at that address, so nothing was done or recorded." in told


def test_a_goto_somewhere_new_is_recorded(harness, scripted_llm, app_url, tmp_path):
    scripted_llm.turns = [[("goto", {"url": "login.html"})], [("step_done", {"evidence": ["Sign in"]})]]
    h = harness(real="browser")
    h.start(_playbook(tmp_path, app_url, [{"id": "go", "do": "Go to the sign-in page"}]), **SCRIPTED)
    assert [(a["type"], a["url"]) for a in h.json("r1", "actions.lock.json")["steps"][0]["actions"]] == [("goto", "login.html")]

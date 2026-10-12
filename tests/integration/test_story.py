"""The story stages on a real take of the fixture app, with the vision and writer models scripted."""

from __future__ import annotations

import struct

import yaml

from tests.conftest import SCRIPTED, Ref

STEP_IDS = ["add-task", "finish-task"]
FACTS = {"steps": [
    {"step_id": "add-task", "happens": "A task called Write the playbook appears, with a Task saved notice.", "on_screen": ["Write the playbook", "1 left", "Task saved"]},
    {"step_id": "finish-task", "happens": "The task is struck through and nothing is left.", "on_screen": ["0 left"]},
]}  # fmt: skip
STORY = {"steps": [{"step_id": "add-task", "text": "Type it, add it."}, {"step_id": "finish-task", "text": "One click, done."}]}


def _png_size(path) -> tuple[int, int]:
    return struct.unpack(">II", path.read_bytes()[16:24])


def test_keyframes_come_from_the_take_and_include_the_passing_toast(harness, scripted_llm, pinned_playbook):
    scripted_llm.replies = [FACTS, STORY]
    h = harness(real=("browser", "story"))
    assert h.start(pinned_playbook, auto_approve=True, **SCRIPTED).phase == "done"

    manifest = h.json("r1", "keyframes/manifest.json")
    windows = h.json("r1", "windows.json")
    assert [entry["step_id"] for entry in manifest["steps"]] == STEP_IDS
    add, finish = manifest["steps"]
    captions = [frame["caption"] for frame in add["frames"]]
    # Two actions, plus the "Task saved" toast that appears on Add and is gone before the step ends.
    assert captions == ['after: fill', "briefly on screen during the step", "after: click"]
    assert [frame["caption"] for frame in finish["frames"]] == ["after: click"]
    for entry in manifest["steps"]:
        window = windows[entry["step_id"]]
        assert entry["before"]["t"] == window["start"]
        assert all(window["start"] < frame["t"] < window["end"] for frame in entry["frames"])
    for frame in (add["before"], *add["frames"], finish["before"], *finish["frames"]):
        assert _png_size(h.path("r1", frame["image"]["path"])) == (800, 600)

    # The vision model saw every frame once, in order, each under its caption.
    shown = "\n".join(scripted_llm.requests[0][1]["content"][i]["text"] for i in range(0, 14) if scripted_llm.requests[0][1]["content"][i]["type"] == "text")
    images = [part for part in scripted_llm.requests[0][1]["content"] if part["type"] == "image_url"]
    assert len(images) == 1 + 3 + 1
    assert "The screen before the first of these steps:" in shown and "briefly on screen during the step:" in shown
    assert "Step `add-task`. The presenter: Add a task called Write the playbook" in shown
    assert "Report on these steps, in this order: add-task, finish-task." in shown

    # The writer was given what the vision model reported, and its story is what gets voiced.
    assert 'On screen: "Write the playbook"; "1 left"; "Task saved"' in scripted_llm.transcript[1]
    assert h.json("r1", "story.json") == STORY
    assert [w["w"] for w in h.json("r1", "words.json")["steps"]["finish-task"]] == ["One", "click,", "done."]
    assert "write_story" not in h.json("r1", "report.json")["stub_stages"]


def test_keyframes_are_scaled_down_for_the_vision_model(harness, scripted_llm, pinned_playbook):
    data = yaml.safe_load(pinned_playbook.read_text())
    data["target"]["viewport"] = {"width": 1000, "height": 700, "device_scale_factor": 2}  # a 2000x1400 video
    pinned_playbook.write_text(yaml.safe_dump(data))
    scripted_llm.replies = [FACTS, STORY]
    h = harness(real=("browser", "story"))
    h.start(pinned_playbook, auto_approve=True, keyframe_max_edge=1000, **SCRIPTED)
    assert _png_size(h.path("r1", "keyframes/add-task/00.png")) == (1000, 700)


def test_overrun_found_after_voicing_goes_back_to_the_writer(harness, scripted_llm, pinned_playbook):
    scripted_llm.replies = [FACTS, STORY, {"steps": [{"step_id": "add-task", "text": "Changed by mistake."}, {"step_id": "finish-task", "text": "Done."}]}]
    flag_once = iter([["finish-task"]])

    def plan_timeline(ctx, inputs):
        from demogen.stages import REGISTRY

        return {**REGISTRY["plan_timeline"].fn(ctx, inputs), "flagged": next(flag_once, [])}

    h = harness({"plan_timeline": plan_timeline}, real=("browser", "story"))
    h.start(pinned_playbook, **SCRIPTED)
    h.runtime.approve("r1")
    status = h.runtime.approve("r1")  # the story; voicing then finds finish-task too long
    assert status.phase == "awaiting_approval" and status.awaiting["kind"] == "story"
    story = h.json("r1", "story.json")
    assert [s["text"] for s in story["steps"]] == ["Type it, add it.", "Done."]  # only the flagged passage changed
    request = scripted_llm.transcript[1]
    assert "- finish-task: took" in request and "Rewrite it in at most 2 words." in request
    assert h.runtime.approve("r1").phase == "done"
    assert h.calls["write_story"] == 2 and h.calls["record_take"] == 1 and h.calls["analyze_frames"] == 1
    assert h.calls["synthesize_step"] == 3  # both passages, then only the rewritten one


def test_secret_shown_on_screen_does_not_reach_the_story(harness, scripted_llm, app_url, tmp_path, monkeypatch):
    monkeypatch.setenv("DEMO_USER", "ada.lovelace")
    monkeypatch.setenv("DEMO_PASS", "correct-horse")
    scripted_llm.turns = [
        [
            ("fill", {"ref": Ref('textbox "Username"'), "text": "${secret:DEMO_USER}"}),
            ("fill", {"ref": Ref('textbox "Password"'), "text": "${secret:DEMO_PASS}"}),
            ("click", {"ref": Ref('button "Sign in"')}),
        ],
        [("step_done", {"evidence": ["Create report"]})],
    ]
    scripted_llm.replies = [
        # The vision model reads the signed-in user's name off the screen.
        {"steps": [{"step_id": "create", "happens": "ada.lovelace sees the report appear.", "on_screen": ["Welcome, ada.lovelace", "Report created"]}]},
        {"steps": [{"step_id": "create", "text": "Reports in one click."}]},
    ]
    playbook = {
        "version": 1,
        "name": "Reports",
        "target": {"base_url": f"{app_url}/private.html", "viewport": {"width": 800, "height": 600, "device_scale_factor": 1}},
        "setup": {"steps": [{"do": "Sign in as ${secret:DEMO_USER} with password ${secret:DEMO_PASS}"}]},
        "steps": [{"id": "create", "do": "Create a report", "action": [{"click": {"role": "button", "name": "Create report"}}]}],
    }
    path = tmp_path / "playbook.yaml"
    path.write_text(yaml.safe_dump(playbook))
    h = harness(real=("browser", "story"))
    assert h.start(path, auto_approve=True, **SCRIPTED).phase == "done"
    facts = h.json("r1", "facts.json")["steps"][0]
    assert facts == {"step_id": "create", "happens": "[REDACTED] sees the report appear.", "on_screen": ["Welcome, [REDACTED]", "Report created"]}
    assert "ada.lovelace" not in scripted_llm.transcript[1]  # the writer's brief

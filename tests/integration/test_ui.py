"""The local app's server: the API over the local backend, the review payloads, and the guard rails."""

from __future__ import annotations

import threading
from dataclasses import replace

import pytest
import yaml
from fastapi.testclient import TestClient

from demogen.exec.local import LocalExecutor
from demogen.ui.local import LocalBackend
from demogen.ui.server import build_app
from tests.conftest import SCRIPTED, all_fakes

DRAFT = {
    "name": "Todos in a hurry", "base_url": "https://demo.playwright.dev/todomvc", "audience": "developers", "tone": "brisk",
    "key_messages": [], "cta": "",
    "steps": [
        {"id": "add", "do": "Add a todo called Ship it", "expect": "It is listed", "say_hint": "Quick.", "dwell_seconds": 2},
        {"id": "tick", "do": "Tick it off", "expect": "It is struck through", "say_hint": "Done.", "dwell_seconds": 2},
    ],
}  # fmt: skip


@pytest.fixture
def app(tmp_path):
    """The local app over a temp runs folder, with every slow stage replaced by its stand-in."""
    made = []

    def make(registry=None, config=None):
        config_path = None
        if config is not None:
            config_path = tmp_path / "demogen.yaml"
            config_path.write_text(yaml.safe_dump(config))
        backend = LocalBackend(tmp_path / "runs", executor=LocalExecutor(registry or all_fakes()), default_config=config_path)
        made.append(backend)
        return backend, TestClient(build_app(backend), base_url="http://127.0.0.1:8765")

    yield make
    for backend in made:
        backend.close()


def _settle(backend, client, run_id):
    assert backend.idle(timeout=30), "the run did not finish its step in time"
    return client.get(f"/api/runs/{run_id}").json()


def _upload(client, playbook, run_id="r1", **form):
    return client.post("/api/runs", files={"playbook": ("todomvc.yaml", playbook.read_bytes())}, data={"run_id": run_id, **form})


def test_upload_review_each_pause_and_finish(app, playbook):
    backend, client = app(config={})
    assert _upload(client, playbook).status_code == 202
    status = _settle(backend, client, "r1")
    assert status["phase"] == "awaiting_approval" and status["awaiting"]["kind"] == "plan"

    plan = client.get("/api/runs/r1/review").json()
    assert plan["kind"] == "plan" and [s["step_id"] for s in plan["steps"]] == ["add-first", "add-second", "complete-first", "filter-active"]
    assert plan["steps"][0]["do"] == "Add a todo called 'Write the playbook'"
    assert client.post("/api/runs/r1/approve", json=plan["plan"]).status_code == 202  # sent back as JSON, as the app does
    status = _settle(backend, client, "r1")
    assert status["awaiting"]["kind"] == "story"

    story = client.get("/api/runs/r1/review").json()
    first = story["steps"][0]
    assert story["kind"] == "story" and first["text"] == "Start by typing what needs doing."
    assert (first["seconds"], first["target"], first["limit"]) == (3.0, 9, 12)  # the stand-in take gives every step 3 s
    edited = story["story"]
    edited["steps"][0]["text"] = "Type what needs doing."
    assert client.post("/api/runs/r1/approve", json=edited).status_code == 202
    done = _settle(backend, client, "r1")
    assert done["phase"] == "done" and done["artifacts"]["report"] == "report.json"
    assert client.get("/api/runs/r1/artifacts/story.json").json()["steps"][0]["text"] == "Type what needs doing."
    assert client.get("/api/runs/r1/review").status_code == 409  # nothing to review once done

    listed = client.get("/api/runs").json()
    assert [(run["run_id"], run["name"], run["phase"], run["finished"]) for run in listed] == [("r1", "TodoMVC in thirty seconds", "done", True)]
    assert (backend.runs_dir / "r1" / "status.json").is_file()


def test_prompt_run_reviews_the_draft_and_takes_feedback(app, scripted_llm):
    scripted_llm.replies = [DRAFT, {**DRAFT, "steps": DRAFT["steps"][:1]}]
    backend, client = app(config=SCRIPTED)
    started = client.post("/api/runs", data={"prompt": "Show adding a todo and ticking it off in TodoMVC", "run_id": "p1"})
    assert started.status_code == 202
    status = _settle(backend, client, "p1")
    assert status["awaiting"]["kind"] == "playbook"
    assert client.get("/api/runs").json()[0]["name"] == "Todos in a hurry"  # named after its playbook once drafted

    review = client.get("/api/runs/p1/review").json()
    assert review["can_feedback"] is True and review["failure"] is None
    assert [s["id"] for s in review["playbook"]["steps"]] == ["add", "tick"] and "id: add" in review["yaml"]

    assert client.post("/api/runs/p1/feedback", json={"feedback": "Only the adding, please."}).status_code == 202
    _settle(backend, client, "p1")
    edited = client.get("/api/runs/p1/review").json()["playbook"]
    assert [s["id"] for s in edited["steps"]] == ["add"]
    edited["steps"][0]["do"] = "Add a todo called Ship it today"
    assert client.post("/api/runs/p1/approve", json=edited).status_code == 202  # sent as JSON, as the app does
    assert _settle(backend, client, "p1")["awaiting"]["kind"] == "plan"
    stored = (backend.runs_dir / "p1" / "playbook.yaml").read_text()
    assert stored.startswith("version: 1") and "Ship it today" in stored  # kept as readable YAML


def test_runs_wait_their_turn(app, playbook):
    gate = threading.Event()
    registry = all_fakes()
    load = registry["load_validate"].fn

    def slow_load(ctx, inputs):
        if ctx.run_id == "first":
            gate.wait(10)
        return load(ctx, inputs)

    registry["load_validate"] = replace(registry["load_validate"], fn=slow_load)
    backend, client = app(registry, config={})
    _upload(client, playbook, "first")
    _upload(client, playbook, "second")
    first, second = client.get("/api/runs/first").json(), client.get("/api/runs/second").json()
    assert (first["phase"], first["queue_position"]) == ("running", 0)
    assert (second["phase"], second["queue_position"]) == ("queued", 1)
    assert client.post("/api/runs/second/approve").status_code == 409  # busy: queued counts
    assert client.delete("/api/runs/first").status_code == 400
    gate.set()
    assert _settle(backend, client, "second")["phase"] == "awaiting_approval"


def test_delete_removes_the_run_and_its_folder(app, playbook):
    backend, client = app(config={})
    _upload(client, playbook)
    _settle(backend, client, "r1")
    assert client.delete("/api/runs/r1").status_code == 204
    assert not (backend.runs_dir / "r1").exists() and client.get("/api/runs").json() == []
    assert client.get("/api/runs/r1").status_code == 404


def test_playbook_check_reports_problems_before_anything_starts(app, playbook):
    _, client = app(config={})
    good = client.post("/api/playbooks/check", files={"playbook": ("todomvc.yaml", playbook.read_bytes())}).json()
    assert good["name"] == "TodoMVC in thirty seconds" and len(good["steps"]) == 4
    bad = client.post("/api/playbooks/check", files={"playbook": ("bad.yaml", b"version: 1\nname: x\ntarget: {base_url: http://x}\nsteps: []\n")})
    assert bad.status_code == 422 and bad.json()["detail"] == "steps: needs at least one entry"
    typo = b"version: 1\nname: x\nsteps:\n  - id: a\n    do: x\n    colour: red\n  - id: b\n"
    problems = client.post("/api/playbooks/check", files={"playbook": ("typo.yaml", typo)}).json()["detail"].splitlines()
    assert problems == ["target: is missing", "step 1 › colour: is not something this file can have (check the spelling)", "step 2 › do: is missing"]
    broken = client.post("/api/playbooks/check", files={"playbook": ("bad.yaml", b"steps: [unclosed\n")})
    assert broken.status_code == 422 and "not valid YAML" in broken.json()["detail"]


def test_health_names_what_to_fix(app, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    _, client = app()  # no demogen.yaml at all
    health = client.get("/api/settings/health").json()
    assert health["ready"] is False and any("demogen.yaml" in problem for problem in health["problems"])
    _, client = app(config={"models": {"planner": {"provider": "anthropic", "model": "m"}}})
    problems = client.get("/api/settings/health").json()["problems"]
    assert any("no model for: vision, writer" in p for p in problems) and any("ANTHROPIC_API_KEY" in p for p in problems)


def test_only_this_machine_may_ask(app):
    backend, _ = app(config={})
    for host in ("evil.example", "192.168.1.20:8765", "localhost.evil.example"):
        refused = TestClient(build_app(backend), base_url=f"http://{host}").get("/api/runs")
        assert refused.status_code == 403
    for host in ("127.0.0.1:8765", "localhost:8765"):
        assert TestClient(build_app(backend), base_url=f"http://{host}").get("/api/runs").status_code == 200


def test_the_app_and_the_sample_store_are_served(app):
    _, client = app(config={})
    assert client.get("/").status_code == 200 and "<title>" in client.get("/").text
    shop = client.get("/examples/shop/")
    assert shop.status_code == 200 and "Harbour Goods" in shop.text


def test_actions_are_described_in_plain_words():
    from demogen.ui.review import describe_action

    def role(role, name):
        return [{"by": "role", "role": role, "name": name}]

    cases = [
        ({"type": "fill", "label": 'textbox "Search"', "text": "hammer"}, "Type “hammer” into the \"Search\" box"),
        ({"type": "click", "label": 'button "Add to cart"'}, 'Click the "Add to cart" button'),
        ({"type": "click", "label": "button", "target": [{"by": "css", "selector": "#add"}]}, "Click a button with no visible name"),
        ({"type": "click", "target": role("radio", "Navy")}, 'Click the "Navy" option'),
        ({"type": "fill", "label": 'textbox "Password"', "text": "${secret:DEMO_PASS}"}, 'Type your demo pass (kept secret) into the "Password" box'),
        ({"type": "select", "label": 'combobox "Size"', "text": "L"}, 'Choose “L” in the "Size" box'),
        ({"type": "hover", "label": 'button "More"'}, 'Point at the "More" button'),
        ({"type": "press", "key": "Enter"}, "Press Enter"),
        ({"type": "goto", "url": "/checkout"}, "Go to /checkout"),
    ]
    for action, words in cases:
        assert describe_action(action) == words


def test_options_change_only_what_they_name(app, playbook):
    import json

    backend, client = app(config={"target_wps": 2.8, "render": {"zoom": 1.3}})
    options = {"auto_approve": True, "render": {"captions": False}, "voice": {"tts": {"voice": "bf_emma"}}}
    _upload(client, playbook, options=json.dumps(options))
    status = _settle(backend, client, "r1")
    assert status["phase"] == "done" and status["started_from"] == "playbook" and status["name"] == "TodoMVC in thirty seconds"
    config = yaml.safe_load((backend.runs_dir / "r1" / "config.resolved.yaml").read_text())
    assert config["auto_approve"] is True and config["render"]["captions"] is False and config["voice"]["tts"]["voice"] == "bf_emma"
    assert config["render"]["zoom"] == 1.3 and config["target_wps"] == 2.8  # from demogen.yaml, untouched

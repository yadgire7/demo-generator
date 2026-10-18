"""The local app in a real browser: a demo from description to video, with an edit at every review."""

from __future__ import annotations

import json
import subprocess
import tempfile
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest
import uvicorn
import yaml
from playwright.sync_api import expect, sync_playwright

from demogen.driver.lockfile import Plan, ResolvedStep
from demogen.exec.local import LocalExecutor
from demogen.playbook.actions import Action
from demogen.stages import REGISTRY
from demogen.ui.local import LocalBackend
from demogen.ui.server import build_app
from tests.conftest import EXAMPLE, FAKES, _free_port

ACTIONS = [Action(type="click", label='button "Filters"', target=[{"by": "role", "role": "button", "name": "Filters"}]), Action(type="press", key="Enter")]


def draft(ctx, inputs):
    playbook = yaml.safe_load(EXAMPLE.read_text())
    return {"playbook_src": ctx.store.put_bytes("playbook.yaml", yaml.safe_dump(playbook, sort_keys=False).encode())}


def rehearse(ctx, inputs):
    steps = [ResolvedStep(step_id=step["id"], source="llm", actions=ACTIONS, expect_text=["Done"]) for step in ctx.store.get_json(inputs["playbook"])["steps"]]
    return {"plan": ctx.store.put_json("actions.lock.json", Plan(steps=steps).model_dump(mode="json")), "rehearsal_failure": None}


def slow_record_take(ctx, inputs):
    time.sleep(2)  # long enough to reload the page while it runs
    return FAKES["browser"]["record_take"](ctx, inputs)


def render(ctx, inputs):
    with tempfile.TemporaryDirectory() as scratch:
        video = Path(scratch) / "final.mp4"
        subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=navy:size=320x180:duration=1", "-pix_fmt", "yuv420p", str(video)], check=True)
        return {"final": {"video": ctx.store.put_file("final.mp4", video), "srt": ctx.store.put_bytes("final.srt", b"1\n00:00:00,000 --> 00:00:01,000\nHello\n")}}


def final_qa(ctx, inputs):
    return {"report": ctx.store.put_json("report.json", {"overrunning_steps": [], "audio_warnings": [], "final_check": {"problems": []}})}


@pytest.fixture
def app_page(tmp_path):
    stand_ins = {name: fn for stages in FAKES.values() for name, fn in stages.items()}
    stand_ins.update(draft_playbook=draft, rehearse=rehearse, record_take=slow_record_take, render=render, final_qa=final_qa)
    registry = {name: replace(spec, fn=stand_ins.get(name, spec.fn)) for name, spec in REGISTRY.items()}
    backend = LocalBackend(tmp_path / "runs", executor=LocalExecutor(registry))
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(build_app(backend), host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.05)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(f"http://127.0.0.1:{port}/")
        yield page, backend
        assert errors == []  # the app's own script never throws
        browser.close()
    server.should_exit = True
    thread.join(timeout=5)
    backend.close()


def test_a_demo_from_description_to_video(app_page):
    page, backend = app_page
    expect(page.get_by_role("heading", name="Make your first demo")).to_be_visible()
    page.get_by_role("link", name="Describe a demo").click()

    page.get_by_label("What should the demo show?").fill("Show how to add todos and tick one off")
    page.get_by_label("Starts at").fill("https://demo.playwright.dev/todomvc")
    page.get_by_role("button", name="Create demo").click()

    # The drafted playbook: change what the first step does.
    expect(page.get_by_role("heading", name="Check the playbook")).to_be_visible()
    first = page.get_by_label("What to do").first
    first.fill("Add a todo called 'Write the playbook' and press Enter")
    page.get_by_role("button", name="Save changes and continue").click()

    # The clicks: take one out.
    expect(page.get_by_role("heading", name="Check the clicks")).to_be_visible()
    expect(page.get_by_text('Click the "Filters" button').first).to_be_visible()
    page.get_by_role("button", name='Remove: Click the "Filters" button').first.click()
    expect(page.get_by_role("button", name='Put back: Click the "Filters" button')).to_have_count(1)
    page.get_by_role("button", name="Save changes and continue").click()

    # While it records, a reload shows the same run in the same place.
    expect(page.get_by_text("Recording the take…")).to_be_visible()
    page.reload()
    expect(page.get_by_text("Recording the take…")).to_be_visible()

    # The narration: rewrite the first line, and watch the counter.
    expect(page.get_by_role("heading", name="Check the narration")).to_be_visible(timeout=15_000)
    line = page.locator(".story-step textarea").first
    line.fill("Type it, press Enter, and it is on the list")
    counter = page.locator(".counter").first
    expect(counter).to_have_text("10 words · aim 9 · max 12 · a little long")
    expect(counter).to_have_class("counter warn")
    page.reload()  # nothing typed is saved until it is approved
    expect(line).not_to_have_value("Type it, press Enter, and it is on the list")
    line.fill("Type it and press Enter")
    page.get_by_role("button", name="Save changes and continue").click()

    # Done: the video, where it is saved, and the run on the home screen.
    expect(page.locator("video")).to_be_visible(timeout=15_000)
    run_id = page.url.rsplit("/", 1)[-1]
    expect(page.get_by_text(f"{backend.runs_dir}/{run_id}/final.mp4")).to_be_visible()
    expect(page.get_by_text("Nothing to check")).to_be_visible()

    folder = backend.runs_dir / run_id
    assert yaml.safe_load((folder / "playbook.yaml").read_text())["steps"][0]["do"] == "Add a todo called 'Write the playbook' and press Enter"
    plan = json.loads((folder / "actions.lock.json").read_text())
    assert [action["type"] for action in plan["steps"][0]["actions"]] == ["press"]
    assert [action["type"] for action in plan["steps"][1]["actions"]] == ["click", "press"]
    assert json.loads((folder / "story.json").read_text())["steps"][0]["text"] == "Type it and press Enter"

    page.get_by_role("link", name="← Your demos").click()
    expect(page.locator(".run-row")).to_have_count(1)
    expect(page.locator(".run-row .pill")).to_have_text("Done")

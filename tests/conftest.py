from __future__ import annotations

import json
import re
import threading
from collections import Counter
from dataclasses import replace
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from demogen.config import Config
from demogen.driver.lockfile import Plan, ResolvedStep
from demogen.exec.local import LocalExecutor
from demogen.graph.runtime import Runtime
from demogen.llm.base import LLMResult, ToolCall, assistant_message
from demogen.plugins import register_plugin
from demogen.stages import REGISTRY
from demogen.story.budget import speech_seconds, word_budget

ROOT = Path(__file__).parent
EXAMPLE = ROOT.parent / "examples" / "playbooks" / "todomvc.yaml"
FAKE_STEP_SECONDS = 3.0


@pytest.fixture
def playbook() -> Path:
    return EXAMPLE


# Stand-ins for the browser stages, so control-plane tests need no browser, network or model.
def fake_setup(ctx, inputs):
    return {"auth_state": None}


def fake_rehearse(ctx, inputs):
    playbook = ctx.store.get_json(inputs["playbook"])
    plan = Plan(steps=[ResolvedStep(step_id=step["id"], source="stub") for step in playbook["steps"]])
    return {"plan": ctx.store.put_json("actions.lock.json", plan.model_dump(mode="json"))}


def fake_record_take(ctx, inputs):
    playbook = ctx.store.get_json(inputs["playbook"])
    events, clock = [], 0.0
    for step in playbook["steps"]:
        events.append({"t": clock, "type": "step_start", "step_id": step["id"]})
        clock += max(step["dwell"], FAKE_STEP_SECONDS)
        events.append({"t": clock, "type": "step_end", "step_id": step["id"]})
    lines = "".join(json.dumps(event) + "\n" for event in events)
    return {"events": ctx.store.put_bytes("events.jsonl", lines.encode()), "video": None, "plan": inputs["plan"]}


# Stand-ins for the story stages, so tests of other stages need no vision or writer model.
def fake_extract_keyframes(ctx, inputs):
    windows = ctx.store.get_json(inputs["windows"])
    return {"keyframes": ctx.store.put_json("keyframes/manifest.json", {"steps": [{"step_id": s} for s in windows]})}


def fake_analyze_frames(ctx, inputs):
    playbook = ctx.store.get_json(inputs["playbook"])
    steps = [{"step_id": s["id"], "happens": s["expect"] or s["do"], "on_screen": []} for s in playbook["steps"]]
    return {"facts": ctx.store.put_json("facts.json", {"steps": steps})}


def fake_write_story(ctx, inputs):
    """Narrate each step with its hint; cut a passage to its word budget when it was flagged as too long."""
    playbook = ctx.store.get_json(inputs["playbook"])
    windows = ctx.store.get_json(inputs["windows"])
    flagged = set(inputs.get("flagged") or [])
    steps = []
    for step in playbook["steps"]:
        text = step["say_hint"] or step["do"]
        if step["id"] in flagged:
            window = windows[step["id"]]
            text = " ".join(text.split()[: word_budget(window["end"] - window["start"], ctx.config.target_wps)])
        steps.append({"step_id": step["id"], "text": text})
    return {"story": ctx.store.put_json("story.json", {"steps": steps})}


# Stand-ins for the voice stages: a length estimated from the word count, and no audio.
def fake_synthesize_step(ctx, inputs):
    return {"audio": None, "duration": speech_seconds(inputs["text"], ctx.config.target_wps), "check": None}


def fake_qa_audio(ctx, inputs):
    story = ctx.store.get_json(inputs["story"])
    words = {"steps": {}, "warnings": []}
    for step in story["steps"]:
        tokens = step["text"].split()
        per_word = inputs["clips"][step["step_id"]]["duration"] / max(len(tokens), 1)
        words["steps"][step["step_id"]] = [
            {"w": token, "start": round(i * per_word, 3), "end": round((i + 1) * per_word, 3)} for i, token in enumerate(tokens)
        ]
    return {"words": ctx.store.put_json("words.json", words)}


FAKES = {
    "browser": {"setup": fake_setup, "rehearse": fake_rehearse, "record_take": fake_record_take},
    "story": {"extract_keyframes": fake_extract_keyframes, "analyze_frames": fake_analyze_frames, "write_story": fake_write_story},
    "voice": {"synthesize_step": fake_synthesize_step, "qa_audio": fake_qa_audio},
}


class Harness:
    """A runtime over a temp directory that counts how often each stage really executes.

    Stage groups not named in `real` ("browser", "story", "voice") are replaced by their stand-ins.
    """

    def __init__(self, runs_dir: Path, overrides: dict | None = None, *, real: tuple[str, ...] | str = ()):
        self.calls: Counter[str] = Counter()
        real = (real,) if isinstance(real, str) else real
        fakes = {name: fn for group, stages in FAKES.items() if group not in real for name, fn in stages.items()}
        self.registry = {
            name: replace(spec, fn=self._counted(name, {**fakes, **(overrides or {})}.get(name, spec.fn)))
            for name, spec in REGISTRY.items()
        }
        self.runtime = Runtime(runs_dir, LocalExecutor(self.registry))
        self.runs_dir = runs_dir

    def _counted(self, name, fn):
        def wrapper(ctx, inputs):
            self.calls[name] += 1
            return fn(ctx, inputs)

        return wrapper

    def start(self, playbook: Path, run_id: str = "r1", intent: Path | None = None, **config):
        return self.runtime.start(playbook, intent, Config(**config), run_id)

    def json(self, run_id: str, path: str):
        return self.runtime._context(run_id).store.get_json(path)

    def path(self, run_id: str, path: str) -> Path:
        return self.runs_dir / run_id / path


@pytest.fixture
def harness(tmp_path):
    made = []

    def make(overrides: dict | None = None, **kwargs) -> Harness:
        made.append(Harness(tmp_path / "runs", overrides, **kwargs))
        return made[-1]

    yield make
    for h in made:
        h.runtime.close()


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


@pytest.fixture(scope="session")
def app_url():
    """The fixture web app in tests/fixtures/app, served on a free local port."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(_QuietHandler, directory=str(ROOT / "fixtures" / "app")))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


class Ref:
    """In a scripted tool call: the ref of the element whose snapshot line contains `label`."""

    def __init__(self, label: str):
        self.label = label


class ScriptedLLM:
    """A model that follows a script.

    `turns` are played back as tool calls, reading element refs off the latest page snapshot.
    `replies` answer requests for a structured reply: each is the reply's content as a dict,
    or a function from the request's messages to that dict.
    """

    def __init__(self, turns: list[list[tuple[str, dict]]] = (), replies: list = ()):
        self.turns = list(turns)
        self.replies = list(replies)
        self.transcript: list[str] = []  # the latest conversation the "model" was shown, as text
        self.requests: list[list[dict]] = []  # every structured request's messages

    def complete(self, messages, *, tools=None, response_schema=None):
        self.transcript = [_text(m.get("content")) for m in messages]
        if response_schema is not None:
            self.requests.append(list(messages))
            if not self.replies:
                raise AssertionError("the script ran out of structured replies")
            reply = self.replies.pop(0)
            payload = reply(messages) if callable(reply) else reply
            text = json.dumps(payload)
            return LLMResult(text=text, parsed=response_schema.model_validate(payload), message=assistant_message(text, []))
        snapshot = next(text for text in reversed(self.transcript) if "Page now" in text)
        if not self.turns:
            raise AssertionError("the script ran out of turns")
        calls = []
        for number, (name, arguments) in enumerate(self.turns.pop(0)):
            resolved = {k: self._ref(v.label, snapshot) if isinstance(v, Ref) else v for k, v in arguments.items()}
            calls.append(ToolCall(id=f"call-{len(self.turns)}-{number}", name=name, arguments=resolved))
        return LLMResult(tool_calls=calls, message=assistant_message(None, calls))

    @staticmethod
    def _ref(label: str, snapshot: str) -> str:
        line = next((line for line in snapshot.splitlines() if label in line), None)
        if line is None:
            raise AssertionError(f"no element labelled {label!r} in the snapshot:\n{snapshot}")
        return re.search(r"\[ref=(\w+)\]", line).group(1)


def _text(content) -> str:
    """A message's text, with each image shown as a marker."""
    if isinstance(content, list):
        return "\n".join(part["text"] if part["type"] == "text" else "<image>" for part in content)
    return str(content)


@pytest.fixture
def scripted_llm():
    """Register a scripted model as the `scripted` provider and return a way to load its script."""
    llm = ScriptedLLM()
    register_plugin("llm", "scripted", lambda model, **params: llm)
    return llm


SCRIPTED = {"models": {role: {"provider": "scripted", "model": "script"} for role in ("planner", "vision", "writer")}}

PINNED = """
version: 1
name: Tasks walkthrough
target:
  base_url: {url}/index.html
  viewport: {{ width: 800, height: 600, device_scale_factor: 1 }}
steps:
  - id: add-task
    do: Add a task called Write the playbook
    say_hint: Type what needs doing and add it.
    action:
      - fill: {{ role: textbox, name: New task, text: Write the playbook }}
      - click: {{ role: button, name: Add }}
  - id: finish-task
    do: Mark it done
    say_hint: One click marks it done.
    action:
      - click: {{ role: checkbox, name: "Done: Write the playbook" }}
"""


@pytest.fixture
def pinned_playbook(app_url, tmp_path) -> Path:
    """A playbook on the fixture app whose every action is pinned, so it needs no model."""
    path = tmp_path / "pinned.yaml"
    path.write_text(PINNED.format(url=app_url))
    return path

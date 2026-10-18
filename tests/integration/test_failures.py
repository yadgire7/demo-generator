"""Fail points: elements that can be found again, and every failure kept with the page as it was."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import yaml
from typer.testing import CliRunner

from demogen.cli import app as cli
from demogen.failures import read_failures
from demogen.graph.runtime import RunError
from tests.conftest import SCRIPTED, Ref

PNG = b"\x89PNG"


def _playbook(tmp_path, base_url, steps):
    data = {"version": 1, "name": "Fixture demo", "target": {"base_url": base_url, "viewport": {"width": 800, "height": 600, "device_scale_factor": 1}}, "steps": steps}
    path = tmp_path / "playbook.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


def _failures(h, run_id="r1"):
    return read_failures(h.runtime.context(run_id).store)


def _file(h, record, name, run_id="r1"):
    return h.path(run_id, record["files"][name]).read_bytes()


def test_an_unnamed_element_is_questioned_once_and_found_again_without_its_random_id(harness, scripted_llm, app_url, tmp_path):
    scripted_llm.turns = [
        [("click", {"ref": Ref("link [ref")})],  # a picture link with no name: questioned
        [("click", {"ref": Ref("link [ref")})],  # no named alternative here, so used anyway
        [("step_done", {"evidence": ["Offer opened"]})],
    ]
    h = harness(real="browser")
    h.start(_playbook(tmp_path, f"{app_url}/index.html", [{"id": "open-offer", "do": "Open the offer", "expect": "The offer is open"}]), **SCRIPTED)
    assert "no accessible name" in "\n".join(scripted_llm.transcript)

    [action] = h.json("r1", "actions.lock.json")["steps"][0]["actions"]
    [locator] = action["target"]
    # The block around the link gets a new random id on every load: the path must not rely on it.
    assert locator["by"] == "css" and "#" not in locator["selector"] and locator["selector"].startswith("body > div:nth-of-type(")

    status = h.runtime.approve("r1")  # the take: a fresh page, a new random id, the same link
    assert status.awaiting["kind"] == "story"

    [record] = _failures(h)
    assert (record["phase"], record["kind"], record["step_outcome"]) == ("rehearsal", "fragile_target", "resolved")
    assert record["tool_call"]["name"] == "click" and record["model"] == "scripted/script"
    assert record["step"] == {"id": "open-offer", "index": 0, "do": "Open the offer", "expect": "The offer is open", "data": None}
    assert record["action"]["label"] == "link" and record["page"]["title"] == "Tasks"
    assert _file(h, record, "screenshot").startswith(PNG)
    assert "link [ref=" in _file(h, record, "observation").decode()  # what the model was looking at
    assert "Offer" not in _file(h, record, "observation").decode()  # ...before it clicked
    assert _file(h, record, "snapshot").decode().startswith("- ")


class _Shop(BaseHTTPRequestHandler):
    """A page that works for the first two browsers to visit, then shows a check to the third."""

    visits = 0
    WORKING = (
        "<title>Shop</title>"
        "<button onclick=\"document.getElementById('s').textContent='Started'\">Start</button><p id=s></p>"
        "<button onclick=\"document.getElementById('n').textContent='Next shown'\">Next</button><p id=n></p>"
    )
    CHECK = "<title>Check you are human</title><h1>Check you are human</h1>"

    def do_GET(self):
        if self.path != "/":
            self.send_error(404)
            return
        type(self).visits += 1
        body = (self.WORKING if self.visits <= 2 else self.CHECK).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def shop_url():
    _Shop.visits = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Shop)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}/"
    server.shutdown()


def test_a_take_and_its_failed_repair_are_both_kept_and_both_reported(harness, scripted_llm, shop_url, tmp_path):
    steps = [
        {"id": "start", "do": "Press Start", "action": [{"click": {"role": "button", "name": "Start"}}]},
        {"id": "next", "do": "Press Next", "expect": "Next is shown"},
    ]
    scripted_llm.turns = [[("click", {"ref": Ref('button "Next"')})], [("step_done", {"evidence": ["Next shown"]})]]
    h = harness(real="browser")
    h.start(_playbook(tmp_path, shop_url, steps), **SCRIPTED)  # visit 1: rehearsal

    plan = h.json("r1", "actions.lock.json")
    plan["steps"][1]["actions"][0]["target"] = [{"by": "role", "role": "button", "name": "Nope"}]
    edit = tmp_path / "plan.yaml"
    edit.write_text(yaml.safe_dump(plan))
    with pytest.raises(RunError) as stopped:
        h.runtime.approve("r1", edit)  # visit 2: the take breaks at step 2; visit 3: the repair finds the check

    message = str(stopped.value)
    assert "step 'next' failed in the take (could not find button \"Next\" to click)" in message
    assert "replaying step 'start' to get back to it, could not find button \"Start\" to click" in message

    take, repair = _failures(h)
    assert (take["phase"], take["kind"], take["step"]["id"], take["step_outcome"]) == ("take", "element_not_found", "next", "failed")
    assert take["attempt"] == {"take": 1} and take["action"]["target"] == [{"by": "role", "role": "button", "name": "Nope"}]
    assert take["done"] == ["Press Start"] and take["page"]["title"] == "Shop" and take["model"] is None
    assert (repair["phase"], repair["kind"], repair["step"]["id"], repair["step_outcome"]) == ("repair", "element_not_found", "start", "failed")
    assert repair["attempt"] == {"take": 1, "repairing": "next"}
    assert repair["page"]["title"] == "Check you are human"  # the evidence of why the repair failed
    assert _file(h, repair, "screenshot").startswith(PNG) and "Check you are human" in _file(h, repair, "snapshot").decode()

    exported = CliRunner().invoke(cli, ["failures", "--runs-dir", str(h.runs_dir)])
    assert exported.exit_code == 0, exported.output
    assert [json.loads(line)["id"] for line in exported.output.splitlines()] == [take["id"], repair["id"]]


def test_the_models_mistakes_are_kept_with_how_the_step_ended(harness, scripted_llm, app_url, tmp_path):
    scripted_llm.turns = [
        [("click", {"ref": "e999"})],  # not on the page
        [("hover", {"ref": Ref('heading "Tasks"')})],  # changes nothing: not recorded
        [("step_failed", {"reason": "there is nothing to archive"})],
    ]
    h = harness(real="browser")
    status = h.start(_playbook(tmp_path, f"{app_url}/index.html", [{"id": "archive", "do": "Archive the first task"}]), **SCRIPTED)
    assert status.awaiting["failure"]["step_id"] == "archive"  # back to the playbook review

    records = _failures(h)
    assert [(r["kind"], r["step_outcome"]) for r in records] == [("bad_ref", "unresolved"), ("no_effect", "unresolved"), ("step_unresolved", "unresolved")]
    assert records[0]["tool_call"] == {"name": "click", "arguments": {"ref": "e999"}}
    assert records[1]["action"]["type"] == "hover" and records[2]["message"] == "there is nothing to archive"
    assert all(r["attempt"] == {"rehearsal": 1} for r in records)

    scripted_llm.turns = [[("click", {"ref": Ref('button "Add"')})], [("step_done", {"evidence": ["0 left"]})]]
    h.runtime.approve("r1")  # rehearsed again, unchanged: a second attempt, and no new fail points
    assert len(_failures(h)) == 3


def test_fail_points_are_served_for_the_app(harness, scripted_llm, app_url, tmp_path):
    from fastapi.testclient import TestClient

    from demogen.exec.local import LocalExecutor
    from demogen.ui.local import LocalBackend
    from demogen.ui.server import build_app

    scripted_llm.turns = [[("click", {"ref": "e999"})], [("step_failed", {"reason": "no"})]]
    h = harness(real="browser")
    h.start(_playbook(tmp_path, f"{app_url}/index.html", [{"id": "archive", "do": "Archive it"}]), **SCRIPTED)
    h.runtime.close()
    backend = LocalBackend(h.runs_dir, executor=LocalExecutor(h.registry))
    try:
        client = TestClient(build_app(backend), base_url="http://127.0.0.1")
        served = client.get("/api/runs/r1/failures").json()
        assert [record["kind"] for record in served] == ["bad_ref", "step_unresolved"]
        picture = client.get(f"/api/runs/r1/artifacts/{served[0]['files']['screenshot']}")
        assert picture.status_code == 200 and picture.headers["content-type"] == "image/png"
    finally:
        backend.close()

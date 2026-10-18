"""The service: runs driven through the job queue by worker pools, and the HTTP API in front."""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import threading
from dataclasses import replace

import psycopg
import pytest
import yaml
from fastapi.testclient import TestClient

from demogen.service.api import create_api
from demogen.service.core import Service
from demogen.service.settings import Settings
from demogen.stages import StageError
from tests.conftest import ROOT, SCRIPTED, all_fakes, wait_for

TOKEN = "test-token"
STAGE_POOLS = {
    "load_validate": "cpu", "setup": "browser", "rehearse": "browser", "record_take": "browser", "verify_take": "cpu",
    "extract_keyframes": "cpu", "analyze_frames": "cpu", "write_story": "cpu", "synthesize_step": "gpu", "qa_audio": "cpu",
    "plan_timeline": "cpu", "render": "cpu", "final_qa": "gpu",
}  # fmt: skip


class Workers:
    """Worker pools running in a thread of this process, on their own event loop."""

    def __init__(self, service: Service, pools: list[str]):
        self._loop = asyncio.new_event_loop()
        self._task = self._loop.create_task(service.serve_pools(pools, 8, signals=False))
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            self._loop.run_until_complete(self._task)
        except asyncio.CancelledError:
            pass

    def stop(self) -> None:
        self._loop.call_soon_threadsafe(self._task.cancel)
        self._thread.join(10)


@pytest.fixture
def settings(database_url, bucket, minio_server, tmp_path):
    return Settings(
        database_url=database_url, s3_bucket=bucket, s3_endpoint=minio_server["endpoint"], s3_prefix="runs",
        runs_dir=tmp_path / "unused", api_token=TOKEN, default_config=None, checkpoint_ttl_days=30,
    )  # fmt: skip


@pytest.fixture
def deployment(settings):
    """Two handles on one deployment, as two processes would hold: the API's, and a worker's with pools running."""
    started = []

    def make(registry=None, pools=("control", "browser", "gpu", "cpu"), **options):
        registry = registry or all_fakes()
        api = Service(settings, registry, **options)
        api.migrate()
        worker = Service(settings, registry, **options)
        opened = [api.queue.open(), worker.queue.open()]
        workers = Workers(worker, list(pools)) if pools else None
        started.append((workers, opened, api, worker))
        return api, worker

    yield make
    for workers, opened, api, worker in started:
        if workers:
            workers.stop()
        for handle in (api, worker):
            handle.queue.connector.get_sync_connector().close()
            handle.runs.close()
            handle.runtime.close()


def _start(service: Service, playbook, run_id="r1", **config):
    from demogen.config import Config

    run_id, step = service.runtime.prepare_start(playbook.read_bytes(), None, Config(**config), run_id, "demo")
    service.runs.add(run_id, "demo")
    service.submit(run_id, step)
    return run_id


def _phase(service: Service, run_id: str, phase: str, kind: str | None = None):
    seen = {}

    def reached():
        seen.update(service.describe(run_id))
        return dict(seen) if seen["phase"] == phase and (kind is None or seen["awaiting"]["kind"] == kind) else None

    try:
        return wait_for(reached, what=f"run {run_id} to be {phase}")
    except AssertionError as error:
        raise AssertionError(f"{error}; it is {seen['phase']} at {seen['next']} (error: {seen['error']})") from None


def _approve(service: Service, run_id: str) -> None:
    service.submit(run_id, service.runtime.prepare_approval(run_id))


def test_run_is_driven_through_the_queue_with_each_stage_in_its_pool(deployment, settings, playbook):
    api, _ = deployment()
    _start(api, playbook)
    _phase(api, "r1", "awaiting_approval", "plan")
    _approve(api, "r1")
    _phase(api, "r1", "awaiting_approval", "story")
    _approve(api, "r1")
    done = _phase(api, "r1", "done")
    assert done["state"]["report"]["path"] == "report.json" and api.runs.get("r1")["finished_at"] is not None

    with psycopg.connect(settings.database_url) as connection:
        ran = connection.execute("SELECT args->>'stage_name', queue_name, status FROM procrastinate_jobs WHERE task_name = 'demogen.run_stage'").fetchall()
    assert {stage: queue for stage, queue, _ in ran} == STAGE_POOLS
    assert all(status == "succeeded" for _, _, status in ran)
    assert sum(stage == "synthesize_step" for stage, _, _ in ran) == 4  # one job per passage


def test_a_stage_waits_for_its_pool_to_have_a_worker(deployment, playbook):
    api, _ = deployment(pools=("control", "cpu"))  # nobody is serving the browser pool yet
    _start(api, playbook, auto_approve=True)
    wait_for(lambda: api.describe("r1")["next"] == ["setup"], what="the run to reach its first browser stage")
    assert api.describe("r1")["phase"] == "running" and api.busy("r1")

    deployment(pools=("browser", "gpu"))  # more workers join later, as another process would
    _phase(api, "r1", "done")


def test_a_failed_stage_is_reported_and_the_run_can_be_resumed(deployment, playbook):
    registry = all_fakes()
    working = registry["qa_audio"].fn
    failures = iter([StageError("the speech checker ran out of memory")])

    def flaky(ctx, inputs):
        for failure in failures:
            raise failure
        return working(ctx, inputs)

    registry["qa_audio"] = replace(registry["qa_audio"], fn=flaky)
    api, _ = deployment(registry)
    _start(api, playbook, auto_approve=True)
    failed = _phase(api, "r1", "failed")
    assert "the speech checker ran out of memory" in failed["error"] and failed["next"] == ["qa_audio"]
    assert not api.busy("r1")

    api.submit("r1", api.runtime.prepare_resume("r1"))
    assert _phase(api, "r1", "done")["error"] is None


def test_steps_for_one_run_are_taken_one_at_a_time(deployment, playbook):
    api, _ = deployment(pools=())  # no workers: jobs stay queued
    _start(api, playbook)
    api.submit("r1", {"kind": "resume"})
    with psycopg.connect(api.settings.database_url) as connection:
        locks = connection.execute("SELECT lock FROM procrastinate_jobs WHERE task_name = 'demogen.drive_run'").fetchall()
    assert locks == [("run:r1",), ("run:r1",)]  # the queue never runs two jobs holding the same lock at once


@pytest.fixture
def client(deployment):
    api, _ = deployment()
    return TestClient(create_api(api, token=TOKEN), headers={"Authorization": f"Bearer {TOKEN}"})


def _wait_http(client: TestClient, run_id: str, phase: str) -> dict:
    return wait_for(lambda: (body := client.get(f"/runs/{run_id}").json())["phase"] == phase and body, what=f"run {run_id} to be {phase}")


def test_api_runs_a_demo_from_upload_to_download(client, playbook):
    started = client.post("/runs", files={"playbook": ("todomvc.yaml", playbook.read_bytes())}, data={"run_id": "web1"})
    assert started.status_code == 202 and started.json()["run_id"] == "web1"

    waiting = _wait_http(client, "web1", "awaiting_approval")
    assert waiting["awaiting"] == {"kind": "plan", "artifact": "actions.lock.json"}
    plan = client.get("/runs/web1/artifacts/actions.lock.json")
    assert plan.status_code == 200 and len(plan.json()["steps"]) == 4
    assert client.post("/runs/web1/approve").status_code == 202

    story = _wait_http(client, "web1", "awaiting_approval")
    assert story["awaiting"]["kind"] == "story"
    edited = yaml.safe_load(client.get("/runs/web1/artifacts/story.json").text)
    edited["steps"][0]["text"] = "An edited opening line."
    assert client.post("/runs/web1/approve", content=yaml.safe_dump(edited)).status_code == 202

    done = _wait_http(client, "web1", "done")
    assert done["artifacts"]["report"] == "report.json" and done["error"] is None
    assert "An edited opening line." in client.get("/runs/web1/artifacts/story.json").text
    assert [run["run_id"] for run in client.get("/runs").json()] == ["web1"]

    rerun = client.post("/runs/web1/rerun", json={"from_stage": "write_story", "intent": "Speak to managers."})
    assert rerun.status_code == 202
    assert _wait_http(client, "web1", "awaiting_approval")["awaiting"]["kind"] == "story"
    assert client.get("/runs/web1/artifacts/intent.json").json()["notes"] == "Speak to managers."


def test_api_refuses_what_it_should(client, playbook):
    assert TestClient(client.app).get("/runs").status_code == 401
    assert TestClient(client.app, headers={"Authorization": "Bearer wrong"}).get("/runs").status_code == 401
    assert TestClient(client.app).get("/healthz/").json() == {"status": "ok"}  # no token needed to ask if it is up

    assert client.get("/runs/nope").status_code == 404
    bad = client.post("/runs", files={"playbook": ("bad.yaml", b"version: 1\nname: x\ntarget: {base_url: http://localhost}\nsteps: []\n")})
    assert bad.status_code == 422 and "steps" in bad.json()["detail"]
    assert client.get("/runs").json() == []  # the rejected playbook left no run behind

    client.post("/runs", files={"playbook": ("todomvc.yaml", playbook.read_bytes())}, data={"run_id": "web2"})
    assert client.post("/runs", files={"playbook": ("todomvc.yaml", playbook.read_bytes())}, data={"run_id": "web2"}).status_code == 409
    _wait_http(client, "web2", "awaiting_approval")
    assert client.post("/runs/web2/resume").status_code == 409  # paused for approval, not stopped
    assert client.post("/runs/web2/rerun", json={"from_stage": "render"}).status_code == 400  # not reached yet
    short = yaml.safe_dump({"steps": []})
    assert client.post("/runs/web2/approve", content=short).status_code == 400  # must cover the playbook's steps
    assert client.get("/runs/web2").json()["phase"] == "awaiting_approval"  # the refused requests changed nothing

    # Saved sign-in sessions and internal files are never served, even when they exist.
    assert client.get("/runs/web2/artifacts/config.resolved.yaml").status_code == 200
    assert client.get("/runs/web2/artifacts/auth/state.json").status_code == 403
    assert client.get("/runs/web2/artifacts/.cache/rehearse/x.json").status_code == 403
    assert client.get("/runs/web2/artifacts/keyframes/.hidden").status_code == 403
    assert client.get("/runs/web2/artifacts/no-such-file.txt").status_code == 404


def test_a_job_whose_worker_dies_is_given_to_another(deployment, settings, playbook, tmp_path, minio_server):
    api, worker = deployment(pools=("control", "gpu", "cpu"), heartbeat_s=0.3, stalled_after_s=1.5)
    flag = tmp_path / "hang"
    flag.touch()
    env = {
        **os.environ, **minio_server["env"], "PYTHONPATH": str(ROOT.parent),
        "DEMOGEN_DATABASE_URL": settings.database_url, "DEMOGEN_S3_BUCKET": settings.s3_bucket,
        "DEMOGEN_S3_ENDPOINT": settings.s3_endpoint, "DEMOGEN_TEST_HANG_FLAG": str(flag),
    }  # fmt: skip
    doomed = subprocess.Popen([sys.executable, "-m", "tests.service_worker", "browser"], env=env, cwd=ROOT.parent)
    replacement = None
    try:
        _start(api, playbook, auto_approve=True)

        def rehearsing():
            with psycopg.connect(settings.database_url) as connection:
                return connection.execute("SELECT id FROM procrastinate_jobs WHERE args->>'stage_name' = 'rehearse' AND status = 'doing'").fetchone()

        wait_for(rehearsing, what="the browser worker to start rehearsing")
        doomed.send_signal(signal.SIGKILL)  # the machine dies mid-stage
        doomed.wait()
        flag.unlink()

        replacement = subprocess.Popen([sys.executable, "-m", "tests.service_worker", "browser"], env=env, cwd=ROOT.parent)
        # Nothing moves until the dead worker's job is noticed and put back in the queue.
        retried = wait_for(lambda: asyncio.run(_reap(settings)), timeout=15, what="the stalled job to be noticed")
        assert retried == 1
        assert _phase(api, "r1", "done")["error"] is None
    finally:
        for process in (doomed, replacement):
            if process and process.poll() is None:
                process.kill()
                process.wait()


async def _reap(settings: Settings) -> int:
    reaper = Service(settings, all_fakes(), heartbeat_s=0.3, stalled_after_s=1.5)
    try:
        async with reaper.queue.open_async():
            return await reaper.reap()
    finally:
        reaper.runs.close()
        reaper.runtime.close()


def test_old_finished_runs_lose_their_checkpoints_but_keep_their_artifacts(deployment, settings, playbook):
    api, _ = deployment()
    _start(api, playbook, auto_approve=True)
    _phase(api, "r1", "done")
    assert asyncio.run(_reap(settings)) == 0 and api.describe("r1")["archived"] is False  # finished just now

    with psycopg.connect(settings.database_url, autocommit=True) as connection:
        connection.execute("UPDATE demogen_runs SET finished_at = now() - interval '31 days'")
    asyncio.run(_reap(settings))
    archived = api.describe("r1")
    assert archived["archived"] is True and archived["phase"] == "done"
    assert api.runtime.context("r1").store.exists("report.json")
    with psycopg.connect(settings.database_url) as connection:
        assert connection.execute("SELECT count(*) FROM checkpoints WHERE thread_id = 'r1'").fetchone()[0] == 0


def test_a_trace_follows_a_run_across_the_queue(deployment, playbook, spans):
    api, _ = deployment()
    _start(api, playbook, auto_approve=True)
    _phase(api, "r1", "done")

    by_name = {span.name: span for span in spans()}
    submit, advance = by_name["run.submit"], by_name["run.advance"]
    assert submit.parent is None and submit.attributes["demogen.step"] == "start"
    assert advance.parent.span_id == submit.context.span_id  # queued by the API, driven by a control worker

    queued, ran = by_name["queue record_take"], by_name["stage record_take"]
    assert queued.parent.span_id == advance.context.span_id and queued.attributes["demogen.pool"] == "browser"
    assert isinstance(queued.attributes["demogen.job_id"], int)
    assert ran.parent.span_id == queued.context.span_id  # on a browser worker, under the wait that queued it
    assert {span.context.trace_id for span in spans()} == {submit.context.trace_id}
    assert sum(span.name == "stage synthesize_step" for span in spans()) == 4


def test_api_drafts_a_playbook_from_a_prompt_and_takes_feedback(deployment, scripted_llm):
    step = {"id": "add", "do": "Add a todo", "expect": "It is listed", "say_hint": "Quick.", "dwell_seconds": 2}
    draft = {"name": "Todos", "base_url": "https://demo.playwright.dev/todomvc", "audience": "developers", "tone": "brisk", "key_messages": [], "cta": "", "steps": [step]}
    scripted_llm.replies = [draft, {**draft, "steps": [step, {**step, "id": "tick", "do": "Tick it off"}]}]
    api, _ = deployment()
    client = TestClient(create_api(api, token=TOKEN), headers={"Authorization": f"Bearer {TOKEN}"})

    started = client.post("/runs", data={"prompt": "Show adding a todo in TodoMVC.", "run_id": "web3"}, files={"config": ("c.yaml", yaml.safe_dump(SCRIPTED).encode())})
    assert started.status_code == 202
    waiting = _wait_http(client, "web3", "awaiting_approval")
    assert waiting["awaiting"] == {"kind": "playbook", "artifact": "playbook.yaml"} and waiting["artifacts"]["playbook"] == "playbook.yaml"
    assert "id: add" in client.get("/runs/web3/artifacts/playbook.yaml").text

    assert client.post("/runs/web3/feedback", json={"feedback": "Also tick it off."}).status_code == 202
    wait_for(lambda: "id: tick" in client.get("/runs/web3/artifacts/playbook.yaml").text, what="the redraft")
    assert _wait_http(client, "web3", "awaiting_approval")["awaiting"]["kind"] == "playbook"

    assert client.post("/runs/web3/approve").status_code == 202
    wait_for(lambda: (client.get("/runs/web3").json()["awaiting"] or {}).get("kind") == "plan", what="the plan approval")
    assert client.post("/runs/web3/feedback", json={"feedback": "Click elsewhere."}).status_code == 400  # only the playbook takes feedback
    assert client.post("/runs", data={"run_id": "web4"}).status_code == 400  # neither a playbook nor a prompt

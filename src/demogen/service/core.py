"""One process's handle on a deployment: storage, checkpoints, the job queue and the list of runs.

The API process prepares changes to runs and queues them. A `control` worker drives each run's
pipeline, handing every stage to the `browser`, `gpu` or `cpu` pool it belongs to.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
from collections.abc import Mapping
from contextlib import contextmanager
from typing import Any

import procrastinate

from demogen.errors import DemogenError
from demogen.exec.local import LocalExecutor
from demogen.exec.queue import STAGE_TASK, QueueExecutor, error_path
from demogen.graph.runtime import RunStatus, Runtime, Step
from demogen.service.registry import Registry
from demogen.service.settings import Settings
from demogen.stages import StageSpec
from demogen.store.stores import LocalStores, S3Stores, Stores

log = logging.getLogger("demogen.service")

DRIVE_TASK = "demogen.drive_run"
REAP_TASK = "demogen.reap"
CONTROL_QUEUE = "control"
POOLS = ("control", "browser", "gpu", "cpu")
HEARTBEAT_S = 10
STALLED_AFTER_S = 60
KEEP_JOBS_HOURS = 72


def _stores(settings: Settings) -> Stores:
    if not settings.s3_bucket:
        return LocalStores(settings.runs_dir)
    import boto3

    return S3Stores(boto3.client("s3", endpoint_url=settings.s3_endpoint), settings.s3_bucket, settings.s3_prefix)


class Service:
    def __init__(
        self,
        settings: Settings,
        registry: Mapping[str, StageSpec] | None = None,
        *,
        heartbeat_s: float = HEARTBEAT_S,
        stalled_after_s: float = STALLED_AFTER_S,
    ):
        self.settings = settings
        self.heartbeat_s = heartbeat_s  # how often a worker reports that it is alive
        self.stalled_after_s = stalled_after_s  # silence after which its jobs are given to another
        self.queue = procrastinate.App(connector=procrastinate.PsycopgConnector(conninfo=settings.database_url))
        self.runs = Registry(settings.database_url)
        executor = QueueExecutor(self._queue_stage, self.runs.job_status, registry)
        self.runtime = Runtime(_stores(settings), settings.database_url, executor)
        self._stage_runner = LocalExecutor(registry)
        self._register_tasks()

    def migrate(self) -> None:
        """Create what the service needs in Postgres. Safe to run again."""
        self.runs.migrate()
        if not self.runs.table_exists("procrastinate_jobs"):
            with self.queue.open():
                self.queue.schema_manager.apply_schema()

    @contextmanager
    def opened(self):
        """Open the queue for this process's own use: queueing jobs and reading their status."""
        with self.queue.open():
            try:
                yield self
            finally:
                self.runs.close()
                self.runtime.close()

    # What the API does.

    def submit(self, run_id: str, step: Step) -> None:
        """Queue a step for a control worker. Steps for one run are taken one at a time."""
        self.runs.set_error(run_id, None)
        self.queue.configure_task(DRIVE_TASK, queue=CONTROL_QUEUE, lock=f"run:{run_id}").defer(run_id=run_id, step=step)

    def busy(self, run_id: str) -> bool:
        """Whether a step for this run is queued or under way."""
        return self.runs.lock_held(f"run:{run_id}")

    def _queue_stage(self, pool: str, **arguments: Any) -> int:
        return self.queue.configure_task(STAGE_TASK, queue=pool).defer(**arguments)

    def describe(self, run_id: str) -> dict[str, Any]:
        """A run's standing, as the API reports it."""
        record = self.runs.get(run_id)
        if record and record["archived_at"]:
            return {"run_id": run_id, "phase": "done", "archived": True, "next": [], "awaiting": None, "error": None, "state": {}}
        status: RunStatus = self.runtime.status(run_id)
        error = record["error"] if record else None
        phase = "running" if self.busy(run_id) else "failed" if status.phase == "incomplete" and error else status.phase
        return {
            "run_id": run_id,
            "phase": phase,
            "archived": False,
            "next": list(status.next),
            "awaiting": status.awaiting,
            "error": error if phase == "failed" else None,
            "state": status.state,
        }

    # What workers do.

    def _register_tasks(self) -> None:
        @self.queue.task(name=STAGE_TASK)
        def run_stage(run_id: str, stage_name: str, inputs: dict[str, Any], key: str) -> None:
            ctx = self.runtime.context(run_id)
            try:
                self._stage_runner.run(stage_name, ctx, inputs)
            except DemogenError as error:
                # The reason travels back to the run through its store; the job itself just fails.
                ctx.store.put_json(error_path(stage_name, key), {"error": str(error)})
                raise

        @self.queue.task(name=DRIVE_TASK)
        def drive_run(run_id: str, step: Step) -> None:
            try:
                self.runtime.advance(run_id, step)
            except DemogenError as error:
                log.warning("run %s stopped: %s", run_id, error)
                self.runs.set_error(run_id, str(error))
                return
            self.runs.set_finished(run_id, self.runtime.status(run_id).phase == "done")

        @self.queue.periodic(cron="* * * * *")
        @self.queue.task(name=REAP_TASK, queue=CONTROL_QUEUE, queueing_lock=REAP_TASK)
        async def reap(timestamp: int) -> None:
            await self.reap()

    async def reap(self) -> int:
        """Put back in the queue any job whose worker died mid-way, and tidy up what has aged out."""
        stalled = list(await self.queue.job_manager.get_stalled_jobs(seconds_since_heartbeat=self.stalled_after_s))
        for job in stalled:
            log.warning("retrying job %s (%s): its worker stopped responding", job.id, job.task_name)
            await self.queue.job_manager.retry_job(job, retry_at=datetime.datetime.now(datetime.timezone.utc))
        await self.queue.job_manager.delete_old_jobs(nb_hours=KEEP_JOBS_HOURS)
        for run_id in self.runs.finished_before(self.settings.checkpoint_ttl_days):
            # The run's artifacts stay; only its step-by-step history goes.
            self.runtime.forget(run_id)
            self.runs.set_archived(run_id)
        return len(stalled)

    async def serve_pools(self, pools: list[str], concurrency: int, *, signals: bool = True) -> None:
        """Take jobs from the given pools until cancelled."""
        async with self.queue.open_async():
            await self.queue.run_worker_async(
                queues=pools, concurrency=concurrency, wait=True, delete_jobs="never",
                install_signal_handlers=signals, update_heartbeat_interval=self.heartbeat_s, stalled_worker_timeout=self.stalled_after_s,
            )  # fmt: skip

    def run_worker(self, pools: list[str], concurrency: int) -> None:
        """Serve the given pools until interrupted."""
        with self.queue.open():  # a control worker queues stage jobs through the sync side
            asyncio.run(self.serve_pools(pools, concurrency))

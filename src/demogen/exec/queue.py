from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from typing import Any

from demogen.exec.base import result_key
from demogen.stages import REGISTRY, StageContext, StageError, StageSpec
from demogen.store.cache import StageCache

POLL_S = 0.25
STAGE_TASK = "demogen.run_stage"


def error_path(stage: str, key: str) -> str:
    return f".errors/{stage}/{key}.json"


class QueueExecutor:
    """Hands each stage to the worker pool it belongs to and waits for the result.

    The job carries only the stage's name and inputs. Its result comes back through the
    run's stage cache, which the worker fills, so nothing large crosses the queue.
    """

    def __init__(
        self,
        defer: Callable[..., int],
        status: Callable[[int], str],
        registry: Mapping[str, StageSpec] | None = None,
    ):
        self._defer = defer  # queue a stage job on a pool; returns the job's id
        self._status = status  # a job's status by id
        self.registry = REGISTRY if registry is None else registry

    def run(self, stage_name: str, ctx: StageContext, inputs: dict[str, Any]) -> dict[str, Any]:
        spec = self.registry[stage_name]
        cache = StageCache(ctx.store)
        key = result_key(spec, ctx, inputs)
        cached = cache.get(spec.name, key)
        if cached is not None:
            return cached

        job_id = self._defer(spec.pool, run_id=ctx.run_id, stage_name=stage_name, inputs=inputs, key=key)
        while True:
            status = self._status(job_id)
            if status == "succeeded":
                result = cache.get(spec.name, key)
                if result is None:
                    raise StageError(f"{stage_name} finished on a worker but left no result")
                return result
            if status in ("failed", "cancelled", "aborted"):
                try:
                    reason = ctx.store.get_json(error_path(spec.name, key))["error"]
                except FileNotFoundError:
                    reason = f"{stage_name} failed on a {spec.pool} worker; see that worker's log"
                raise StageError(reason)
            time.sleep(POLL_S)

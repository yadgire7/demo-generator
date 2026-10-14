from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from demogen import telemetry
from demogen.exec.base import result_key
from demogen.stages import REGISTRY, StageContext, StageSpec
from demogen.store.cache import StageCache


class LocalExecutor:
    """Runs stages in this process, skipping any whose result is already cached."""

    def __init__(self, registry: Mapping[str, StageSpec] | None = None):
        self.registry = REGISTRY if registry is None else registry

    def run(self, stage_name: str, ctx: StageContext, inputs: dict[str, Any]) -> dict[str, Any]:
        spec = self.registry[stage_name]
        with telemetry.span(f"stage {spec.name}", demogen__run_id=ctx.run_id, demogen__stage=spec.name, demogen__pool=spec.pool) as span:
            cache = StageCache(ctx.store)
            key = result_key(spec, ctx, inputs)
            cached = cache.get(spec.name, key)
            span.set_attribute("demogen.cache_hit", cached is not None)
            if cached is not None:
                return cached
            outputs = spec.fn(ctx, inputs)
            cache.put(spec.name, key, outputs)
            return outputs

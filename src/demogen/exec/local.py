from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from demogen.stages import REGISTRY, StageContext, StageSpec
from demogen.store.cache import StageCache, cache_key


class LocalExecutor:
    """Runs stages in this process, skipping any whose result is already cached."""

    def __init__(self, registry: Mapping[str, StageSpec] | None = None):
        self.registry = REGISTRY if registry is None else registry

    def run(self, stage_name: str, ctx: StageContext, inputs: dict[str, Any]) -> dict[str, Any]:
        spec = self.registry[stage_name]
        cache = StageCache(ctx.store)
        key = cache_key(spec.name, spec.version, ctx.config.fingerprint(spec.config_keys), inputs)
        cached = cache.get(spec.name, key)
        if cached is not None:
            return cached
        outputs = spec.fn(ctx, inputs)
        cache.put(spec.name, key, outputs)
        return outputs

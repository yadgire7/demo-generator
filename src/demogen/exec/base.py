"""Where a stage runs. The control plane asks an executor; it never calls a stage directly."""

from __future__ import annotations

from typing import Any, Protocol

from demogen.stages.base import StageContext, StageSpec
from demogen.store.cache import cache_key


class StageExecutor(Protocol):
    def run(self, stage_name: str, ctx: StageContext, inputs: dict[str, Any]) -> dict[str, Any]: ...


def result_key(spec: StageSpec, ctx: StageContext, inputs: dict[str, Any]) -> str:
    """The cache key under which this stage's result for these inputs is stored."""
    return cache_key(spec.name, spec.version, ctx.config.fingerprint(spec.config_keys), inputs)

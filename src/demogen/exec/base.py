"""Where a stage runs. The control plane asks an executor; it never calls a stage directly."""

from __future__ import annotations

from typing import Any, Protocol

from demogen.stages.base import StageContext


class StageExecutor(Protocol):
    def run(self, stage_name: str, ctx: StageContext, inputs: dict[str, Any]) -> dict[str, Any]: ...

"""A stage is a plain function over artifact refs. It knows nothing about the orchestration engine."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from demogen.config import Config
from demogen.errors import DemogenError
from demogen.store.artifacts import ArtifactStore

Pool = Literal["cpu", "browser", "gpu"]


class StageError(DemogenError):
    pass


@dataclass
class StageContext:
    run_id: str
    store: ArtifactStore
    config: Config


StageFn = Callable[[StageContext, dict[str, Any]], dict[str, Any]]


@dataclass(frozen=True)
class StageSpec:
    name: str
    fn: StageFn
    version: int = 1
    pool: Pool = "cpu"
    config_keys: tuple[str, ...] = ()


REGISTRY: dict[str, StageSpec] = {}


def stage(
    name: str,
    *,
    version: int = 1,
    pool: Pool = "cpu",
    config_keys: tuple[str, ...] = (),
) -> Callable[[StageFn], StageFn]:
    """Register a stage. Bump `version` whenever its output for the same inputs would change."""

    def register(fn: StageFn) -> StageFn:
        REGISTRY[name] = StageSpec(name, fn, version, pool, config_keys)
        return fn

    return register

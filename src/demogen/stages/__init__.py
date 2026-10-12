"""Importing this package registers every pipeline stage."""

from demogen.stages import edit, ingest, record, story, voice  # noqa: F401
from demogen.stages.base import REGISTRY, StageContext, StageError, StageSpec, stage

__all__ = ["REGISTRY", "StageContext", "StageError", "StageSpec", "stage"]

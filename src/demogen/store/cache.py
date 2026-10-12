"""Content-addressed stage results: the same stage, inputs and config never run twice."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from demogen.store.artifacts import ArtifactStore, is_ref

CACHE_DIR = ".cache"


def cache_key(stage: str, version: int, config: dict[str, Any], inputs: dict[str, Any]) -> str:
    payload = json.dumps(
        {"stage": stage, "version": version, "config": config, "inputs": inputs}, sort_keys=True
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _refs(value: Any):
    if is_ref(value):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _refs(item)
    elif isinstance(value, list):
        for item in value:
            yield from _refs(item)


class StageCache:
    def __init__(self, store: ArtifactStore):
        self.store = store

    def _path(self, stage: str, key: str) -> str:
        return f"{CACHE_DIR}/{stage}/{key}.json"

    def get(self, stage: str, key: str) -> dict[str, Any] | None:
        try:
            outputs = self.store.get_json(self._path(stage, key))
        except FileNotFoundError:
            return None
        # A hit only counts while every artifact it points at is still the one it produced.
        if all(self.store.verify(ref) for ref in _refs(outputs)):
            return outputs
        return None

    def put(self, stage: str, key: str, outputs: dict[str, Any]) -> None:
        self.store.put_json(self._path(stage, key), outputs)

    def invalidate(self, stage: str) -> None:
        self.store.delete_prefix(f"{CACHE_DIR}/{stage}")

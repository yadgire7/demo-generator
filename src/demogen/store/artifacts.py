"""Artifacts live in a store; run state carries only references to them."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any, Protocol, TypedDict


class ArtifactRef(TypedDict):
    path: str
    sha256: str


def is_ref(value: Any) -> bool:
    return isinstance(value, dict) and value.keys() == {"path", "sha256"}


def _file_sha256(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


class ArtifactStore(Protocol):
    def put_bytes(self, path: str, data: bytes) -> ArtifactRef: ...
    def get_bytes(self, path: str) -> bytes: ...
    def put_file(self, path: str, source: Path) -> ArtifactRef: ...
    def local_path(self, ref: ArtifactRef) -> Path: ...
    def verify(self, ref: ArtifactRef) -> bool: ...
    def delete_prefix(self, prefix: str) -> None: ...

    def put_json(self, path: str, obj: Any) -> ArtifactRef:
        return self.put_bytes(path, json.dumps(obj, indent=2, sort_keys=True).encode())

    def get_json(self, ref_or_path: ArtifactRef | str) -> Any:
        path = ref_or_path if isinstance(ref_or_path, str) else ref_or_path["path"]
        return json.loads(self.get_bytes(path))


class LocalArtifactStore(ArtifactStore):
    """One directory per run. Paths are relative to it."""

    def __init__(self, root: Path):
        self.root = root

    def _resolve(self, path: str) -> Path:
        full = (self.root / path).resolve()
        if not full.is_relative_to(self.root.resolve()):
            raise ValueError(f"artifact path escapes the run directory: {path}")
        return full

    def put_bytes(self, path: str, data: bytes) -> ArtifactRef:
        full = self._resolve(path)
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_bytes(data)
        return {"path": path, "sha256": hashlib.sha256(data).hexdigest()}

    def put_file(self, path: str, source: Path) -> ArtifactRef:
        full = self._resolve(path)
        full.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, full)
        return {"path": path, "sha256": _file_sha256(full)}

    def local_path(self, ref: ArtifactRef) -> Path:
        """A filesystem path for tools (ffmpeg, ffprobe) that need to open the artifact themselves."""
        return self._resolve(ref["path"])

    def get_bytes(self, path: str) -> bytes:
        return self._resolve(path).read_bytes()

    def verify(self, ref: ArtifactRef) -> bool:
        full = self._resolve(ref["path"])
        return full.is_file() and _file_sha256(full) == ref["sha256"]

    def delete_prefix(self, prefix: str) -> None:
        shutil.rmtree(self._resolve(prefix), ignore_errors=True)

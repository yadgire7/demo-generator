"""Where runs keep their artifacts: a folder per run on this machine, or a prefix per run in a bucket."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from demogen.store.artifacts import ArtifactStore, LocalArtifactStore

# Every run has its settings stored under this name; its presence is what makes a run exist.
CONFIG_FILE = "config.resolved.yaml"


class Stores(Protocol):
    def for_run(self, run_id: str) -> ArtifactStore: ...
    def exists(self, run_id: str) -> bool: ...
    def describe(self) -> str: ...


class LocalStores:
    def __init__(self, runs_dir: Path):
        self.runs_dir = runs_dir
        runs_dir.mkdir(parents=True, exist_ok=True)

    def for_run(self, run_id: str) -> ArtifactStore:
        return LocalArtifactStore(self.runs_dir / run_id)

    def exists(self, run_id: str) -> bool:
        return (self.runs_dir / run_id / CONFIG_FILE).is_file()

    def describe(self) -> str:
        return str(self.runs_dir)


class S3Stores:
    def __init__(self, client: Any, bucket: str, prefix: str = "runs"):
        self.client = client
        self.bucket = bucket
        self.prefix = prefix.strip("/")

    def for_run(self, run_id: str) -> ArtifactStore:
        from demogen.store.s3 import S3ArtifactStore

        return S3ArtifactStore(self.client, self.bucket, f"{self.prefix}/{run_id}")

    def exists(self, run_id: str) -> bool:
        return self.for_run(run_id).exists(CONFIG_FILE)

    def describe(self) -> str:
        return f"s3://{self.bucket}/{self.prefix}"

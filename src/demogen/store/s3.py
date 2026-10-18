"""Artifacts in an S3-compatible bucket (AWS S3, MinIO, R2): what lets workers on different machines share a run."""

from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path
from typing import Any

from demogen.store.artifacts import ArtifactRef, ArtifactStore, _file_sha256

SHA_KEY = "sha256"


class S3ArtifactStore(ArtifactStore):
    """One key prefix per run. Each object carries its content hash, so a ref can be verified without a download."""

    def __init__(self, client: Any, bucket: str, prefix: str):
        self.client = client
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        # Files a tool must open by path (ffmpeg, the browser's saved sign-in) are fetched here once.
        self._scratch = Path(tempfile.gettempdir()) / "demogen-artifacts"

    def _key(self, path: str) -> str:
        if path.startswith("/") or ".." in path.split("/"):
            raise ValueError(f"artifact path escapes the run: {path}")
        return f"{self.prefix}/{path}"

    def _missing(self, error: Exception) -> bool:
        code = getattr(error, "response", {}).get("Error", {}).get("Code")
        return code in ("NoSuchKey", "404", "NotFound")

    def put_bytes(self, path: str, data: bytes) -> ArtifactRef:
        sha = hashlib.sha256(data).hexdigest()
        self.client.put_object(Bucket=self.bucket, Key=self._key(path), Body=data, Metadata={SHA_KEY: sha})
        return {"path": path, "sha256": sha}

    def put_file(self, path: str, source: Path) -> ArtifactRef:
        sha = _file_sha256(source)
        self.client.upload_file(str(source), self.bucket, self._key(path), ExtraArgs={"Metadata": {SHA_KEY: sha}})
        return {"path": path, "sha256": sha}

    def get_bytes(self, path: str) -> bytes:
        try:
            return self.client.get_object(Bucket=self.bucket, Key=self._key(path))["Body"].read()
        except self.client.exceptions.ClientError as error:
            if self._missing(error):
                raise FileNotFoundError(path) from error
            raise

    def local_path(self, ref: ArtifactRef) -> Path:
        # Named by content, so a changed artifact is never served from an old download.
        local = self._scratch / f"{ref['sha256'][:16]}-{Path(ref['path']).name}"
        if not local.is_file():
            local.parent.mkdir(parents=True, exist_ok=True)
            partial = local.with_suffix(local.suffix + ".part")
            self.client.download_file(self.bucket, self._key(ref["path"]), str(partial))
            partial.replace(local)
        return local

    def sha256(self, path: str) -> str:
        try:
            head = self.client.head_object(Bucket=self.bucket, Key=self._key(path))
        except self.client.exceptions.ClientError as error:
            if self._missing(error):
                raise FileNotFoundError(path) from error
            raise
        return head["Metadata"][SHA_KEY]

    def verify(self, ref: ArtifactRef) -> bool:
        try:
            return self.sha256(ref["path"]) == ref["sha256"]
        except FileNotFoundError:
            return False

    def exists(self, path: str) -> bool:
        try:
            self.sha256(path)
        except FileNotFoundError:
            return False
        return True

    def list_prefix(self, prefix: str) -> list[str]:
        """The paths of every artifact under `prefix`, sorted."""
        start = self.prefix + "/"
        under = start + prefix.strip("/") + "/" if prefix.strip("/") else start
        pages = self.client.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=under)
        return sorted(item["Key"].removeprefix(start) for page in pages for item in page.get("Contents", []))

    def delete_prefix(self, prefix: str) -> None:
        pages = self.client.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=self._key(prefix).rstrip("/") + "/")
        for page in pages:
            keys = [{"Key": item["Key"]} for item in page.get("Contents", [])]
            if keys:
                self.client.delete_objects(Bucket=self.bucket, Delete={"Objects": keys})

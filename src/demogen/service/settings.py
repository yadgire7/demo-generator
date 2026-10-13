from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from demogen.errors import DemogenError


@dataclass(frozen=True)
class Settings:
    """How this deployment is wired, read from the environment."""

    database_url: str  # Postgres: checkpoints, the job queue and the list of runs
    s3_bucket: str | None  # artifacts go here; unset keeps them in `runs_dir` (one machine only)
    s3_endpoint: str | None  # for MinIO and other S3-compatible stores; unset means AWS
    s3_prefix: str
    runs_dir: Path
    api_token: str | None
    default_config: Path | None  # settings for runs that are started without their own
    checkpoint_ttl_days: int

    @classmethod
    def from_env(cls) -> Settings:
        env = os.environ
        if not env.get("DEMOGEN_DATABASE_URL"):
            raise DemogenError("DEMOGEN_DATABASE_URL is not set (a postgresql:// URL)")
        return cls(
            database_url=env["DEMOGEN_DATABASE_URL"],
            s3_bucket=env.get("DEMOGEN_S3_BUCKET") or None,
            s3_endpoint=env.get("DEMOGEN_S3_ENDPOINT") or None,
            s3_prefix=env.get("DEMOGEN_S3_PREFIX", "runs"),
            runs_dir=Path(env.get("DEMOGEN_RUNS_DIR", "runs")),
            api_token=env.get("DEMOGEN_API_TOKEN") or None,
            default_config=Path(env["DEMOGEN_CONFIG"]) if env.get("DEMOGEN_CONFIG") else None,
            checkpoint_ttl_days=int(env.get("DEMOGEN_CHECKPOINT_TTL_DAYS", "30")),
        )

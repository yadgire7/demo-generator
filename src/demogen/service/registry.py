"""The list of runs the service knows about, with what last went wrong for each."""

from __future__ import annotations

from typing import Any

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

SCHEMA = """
CREATE TABLE IF NOT EXISTS demogen_runs (
    run_id      text PRIMARY KEY,
    name        text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    archived_at timestamptz,
    error       text
)
"""


class Registry:
    def __init__(self, database_url: str):
        self._pool = ConnectionPool(database_url, kwargs={"autocommit": True, "row_factory": dict_row}, min_size=1, max_size=4, open=True)

    def close(self) -> None:
        self._pool.close()

    def _execute(self, query: str, *params: Any) -> list[dict[str, Any]]:
        with self._pool.connection() as connection:
            cursor = connection.execute(query, params)
            return cursor.fetchall() if cursor.description else []

    def migrate(self) -> None:
        self._execute(SCHEMA)

    def table_exists(self, table: str) -> bool:
        return self._execute("SELECT to_regclass(%s) AS found", table)[0]["found"] is not None

    def add(self, run_id: str, name: str) -> None:
        self._execute("INSERT INTO demogen_runs (run_id, name) VALUES (%s, %s)", run_id, name)

    def get(self, run_id: str) -> dict[str, Any] | None:
        rows = self._execute("SELECT * FROM demogen_runs WHERE run_id = %s", run_id)
        return rows[0] if rows else None

    def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        return self._execute("SELECT * FROM demogen_runs ORDER BY created_at DESC LIMIT %s", limit)

    def set_error(self, run_id: str, error: str | None) -> None:
        self._execute("UPDATE demogen_runs SET error = %s WHERE run_id = %s", error, run_id)

    def set_finished(self, run_id: str, finished: bool) -> None:
        self._execute("UPDATE demogen_runs SET finished_at = CASE WHEN %s THEN now() END WHERE run_id = %s", finished, run_id)

    def finished_before(self, days: int) -> list[str]:
        """Runs that finished more than `days` ago and still have their checkpoints."""
        rows = self._execute(
            "SELECT run_id FROM demogen_runs WHERE archived_at IS NULL AND finished_at < now() - make_interval(days => %s)", days
        )
        return [row["run_id"] for row in rows]

    def set_archived(self, run_id: str) -> None:
        self._execute("UPDATE demogen_runs SET archived_at = now() WHERE run_id = %s", run_id)

    # The job queue's own table, read directly: its client library's blocking calls cannot be
    # used from inside a worker, where a run's driver waits on the stages it has queued.

    def job_status(self, job_id: int) -> str:
        return self._execute("SELECT status FROM procrastinate_jobs WHERE id = %s", job_id)[0]["status"]

    def lock_held(self, lock: str) -> bool:
        """Whether any job holding this lock is queued or under way."""
        return bool(self._execute("SELECT 1 FROM procrastinate_jobs WHERE lock = %s AND status IN ('todo', 'doing') LIMIT 1", lock))

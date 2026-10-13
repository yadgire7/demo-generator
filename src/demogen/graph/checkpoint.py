"""Where run state is checkpointed: a SQLite file for the command line, Postgres for the service."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

from langgraph.checkpoint.sqlite import SqliteSaver


def open_checkpointer(target: str | Path) -> tuple[Any, Callable[[], None]]:
    """A checkpointer for a SQLite path or a `postgresql://` URL, and the function that closes it."""
    if isinstance(target, str) and target.startswith(("postgres://", "postgresql://")):
        from langgraph.checkpoint.postgres import PostgresSaver
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        pool = ConnectionPool(target, kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row}, open=True)
        saver = PostgresSaver(pool)
        saver.setup()  # creates the checkpoint tables on first use; a no-op afterwards
        return saver, pool.close
    connection = sqlite3.connect(target, check_same_thread=False)
    return SqliteSaver(connection), connection.close

"""Run demos on this machine, one at a time, for the local app.

Everything a run needs lives in its folder under `runs/`. The list of runs is read from those
folders, so runs started from the command line appear here too, and nothing is lost when the
app is restarted.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import threading
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from demogen.errors import DemogenError
from demogen.exec.base import StageExecutor
from demogen.graph.runtime import RunError, Runtime, Step
from demogen.store.stores import CONFIG_FILE

log = logging.getLogger("demogen.ui")

STATUS_FILE = "status.json"  # the run's name, when it began, and why it last stopped, if it did


class LocalRunList:
    """The runs in a folder, each with a small status file beside its artifacts."""

    def __init__(self, runs_dir: Path):
        self.runs_dir = runs_dir

    def _status_path(self, run_id: str) -> Path:
        return self.runs_dir / run_id / STATUS_FILE

    def _read(self, run_id: str) -> dict[str, Any]:
        try:
            return json.loads(self._status_path(run_id).read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _write(self, run_id: str, **changes: Any) -> None:
        path = self._status_path(run_id)
        if not path.parent.is_dir():
            return
        record = {**self._read(run_id), **changes}
        path.write_text(json.dumps(record, indent=2))

    def add(self, run_id: str, name: str) -> None:
        self._write(run_id, name=name, created_at=_now(), finished_at=None, error=None)

    def get(self, run_id: str) -> dict[str, Any] | None:
        folder = self.runs_dir / run_id
        if not (folder / CONFIG_FILE).is_file():
            return None
        record = self._read(run_id)
        created = record.get("created_at") or datetime.fromtimestamp((folder / CONFIG_FILE).stat().st_mtime, timezone.utc).isoformat()
        return {
            "run_id": run_id,
            "name": _display_name(folder, record.get("name")),
            "created_at": datetime.fromisoformat(created),
            "finished_at": datetime.fromisoformat(record["finished_at"]) if record.get("finished_at") else None,
            "archived_at": None,
            "error": record.get("error"),
        }

    def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        folders = [path.name for path in self.runs_dir.iterdir() if (path / CONFIG_FILE).is_file()] if self.runs_dir.is_dir() else []
        records = [record for record in (self.get(run_id) for run_id in folders) if record]
        return sorted(records, key=lambda record: record["created_at"], reverse=True)[:limit]

    def set_error(self, run_id: str, error: str | None) -> None:
        self._write(run_id, error=error)

    def set_finished(self, run_id: str, finished: bool) -> None:
        self._write(run_id, finished_at=_now() if finished else None)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _display_name(folder: Path, recorded: str | None) -> str:
    """The playbook's own name once there is one (drafted or uploaded); otherwise what the run was started with."""
    for source, read in (("playbook.json", json.loads), ("playbook.yaml", yaml.safe_load)):
        try:
            name = read((folder / source).read_text())["name"]
            if isinstance(name, str) and name.strip():
                return name
        except (FileNotFoundError, KeyError, TypeError, ValueError, yaml.YAMLError):
            continue
    if recorded:
        return recorded
    prompt = folder / "prompt.md"
    if prompt.is_file():
        return name_from_prompt(prompt.read_text())
    return folder.name


def name_from_prompt(prompt: str, words: int = 8) -> str:
    text = " ".join(prompt.split())
    head = " ".join(text.split()[:words])
    return head + ("…" if len(text) > len(head) else "")


class LocalBackend:
    """Runs on this machine. Steps are taken one at a time, in the order they were asked for."""

    def __init__(self, runs_dir: Path, *, executor: StageExecutor | None = None, default_config: Path | None = None):
        self.runs_dir = runs_dir
        self.default_config = default_config
        self.runtime = Runtime.local(runs_dir, executor)
        self.runs = LocalRunList(runs_dir)
        self._waiting: deque[tuple[str, Step]] = deque()
        self._current: str | None = None
        self._changed = threading.Condition()
        self._stopping = False
        self._worker = threading.Thread(target=self._work, name="demogen-runs", daemon=True)
        self._worker.start()

    def close(self) -> None:
        with self._changed:
            self._stopping = True
            self._changed.notify_all()
        self._worker.join(timeout=5)
        self.runtime.close()

    def submit(self, run_id: str, step: Step) -> None:
        self.runs.set_error(run_id, None)
        with self._changed:
            self._waiting.append((run_id, step))
            self._changed.notify_all()

    def busy(self, run_id: str) -> bool:
        with self._changed:
            return run_id == self._current or any(waiting == run_id for waiting, _ in self._waiting)

    def queue_position(self, run_id: str) -> int | None:
        """0 while the run is being worked on, 1 or more while it waits its turn, None otherwise."""
        with self._changed:
            if run_id == self._current:
                return 0
            for position, (waiting, _) in enumerate(self._waiting, start=1):
                if waiting == run_id:
                    return position
        return None

    def idle(self, timeout: float | None = None) -> bool:
        """Wait until nothing is running or queued. Returns whether that happened in time."""
        with self._changed:
            return self._changed.wait_for(lambda: self._current is None and not self._waiting, timeout)

    def _work(self) -> None:
        while True:
            with self._changed:
                self._changed.wait_for(lambda: self._waiting or self._stopping)
                if self._stopping:
                    return
                run_id, step = self._waiting.popleft()
                self._current = run_id
                self._changed.notify_all()
            try:
                self.runtime.advance(run_id, step)
                self.runs.set_finished(run_id, self.runtime.status(run_id).phase == "done")
            except DemogenError as error:
                log.warning("run %s stopped: %s", run_id, error)
                self.runs.set_error(run_id, str(error))
            except Exception as error:  # a bug: keep the app alive and say what happened
                log.exception("run %s failed unexpectedly", run_id)
                self.runs.set_error(run_id, f"Something went wrong inside demogen: {error!r}. The run is saved; you can resume it.")
            finally:
                with self._changed:
                    self._current = None
                    self._changed.notify_all()

    def describe(self, run_id: str) -> dict[str, Any]:
        status = self.runtime.status(run_id)
        record = self.runs.get(run_id) or {}
        position = self.queue_position(run_id)
        error = record.get("error")
        if position == 0:
            phase = "running"
        elif position:
            phase = "queued"
        elif status.phase == "incomplete" and error:
            phase = "failed"
        elif status.phase == "incomplete" and status.state:
            phase = "interrupted"  # it was under way when the app last stopped
        else:
            phase = status.phase
        return {
            "run_id": run_id,
            "phase": phase,
            "archived": False,
            "queue_position": position,
            "next": list(status.next),
            "awaiting": status.awaiting,
            "error": error if phase == "failed" else None,
            "state": status.state,
        }

    def delete(self, run_id: str) -> None:
        if self.busy(run_id):
            raise RunError(f"run {run_id} is busy; wait for it to pause or finish before deleting it")
        self.runtime.context(run_id)  # refuses an unknown run
        self.runtime.forget(run_id)
        self.runtime.stores.for_run(run_id).delete_prefix("")

    def reveal(self, run_id: str) -> None:
        folder = self.runs_dir / run_id
        if not folder.is_dir():
            raise RunError(f"no run named {run_id!r} in {self.runs_dir}")
        opener = {"darwin": ["open"], "win32": ["explorer"]}.get(sys.platform, ["xdg-open"])
        subprocess.run([*opener, str(folder.resolve())], check=False)

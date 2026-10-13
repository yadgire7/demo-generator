from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Annotated, Optional

import typer
import yaml
from pydantic import ValidationError

from demogen.config import load_config
from demogen.env import load_env
from demogen.errors import DemogenError
from demogen.graph.runtime import RunStatus, Runtime

app = typer.Typer(no_args_is_help=True, add_completion=False, help="Generate narrated product demos from a playbook.")

RunsDir = Annotated[
    Path, typer.Option("--runs-dir", envvar="DEMOGEN_RUNS_DIR", help="Where runs and their checkpoints live.")
]
DEFAULT_CONFIG = Path("demogen.yaml")


def _report(status: RunStatus, runs_dir: Path) -> None:
    typer.echo(f"run     {status.run_id}")
    typer.echo(f"folder  {runs_dir / status.run_id}")
    if status.phase == "awaiting_approval":
        kind, artifact = status.awaiting["kind"], status.awaiting["artifact"]["path"]
        typer.echo(f"status  waiting for you to approve the {kind}: {runs_dir / status.run_id / artifact}")
        typer.echo(f"next    demogen approve {status.run_id}   (add --edit FILE to replace the {kind})")
    elif status.phase == "incomplete":
        typer.echo(f"status  stopped before: {', '.join(status.next) or 'the first stage'}")
        typer.echo(f"next    demogen resume {status.run_id}")
    else:
        folder = runs_dir / status.run_id
        final = status.state["final"].get("video")
        typer.echo(f"status  done: {folder / (final or status.state['report'])['path']}")
        report = json.loads((folder / status.state["report"]["path"]).read_text())
        concerns = len(report["audio_warnings"]) + len(report["overrunning_steps"]) + len(report.get("final_check", {}).get("problems", []))
        typer.echo(f"report  {folder / status.state['report']['path']}" + (f"   ({concerns} thing(s) to look at)" if concerns else ""))


def _run(runs_dir: Path, action) -> None:
    load_env()
    runtime = Runtime.local(runs_dir)
    try:
        _report(action(runtime), runs_dir)
    except (DemogenError, ValidationError, yaml.YAMLError) as error:
        typer.echo(f"error: {error}", err=True)
        raise typer.Exit(1)
    finally:
        runtime.close()


@app.command()
def run(
    playbook: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    intent: Annotated[Optional[Path], typer.Option(exists=True, dir_okay=False, help="The author's brief (markdown).")] = None,
    config: Annotated[Optional[Path], typer.Option(exists=True, dir_okay=False, help="Defaults to ./demogen.yaml if present.")] = None,
    auto_approve: Annotated[bool, typer.Option("--auto-approve", help="Do not pause for plan and story approval.")] = False,
    run_id: Annotated[Optional[str], typer.Option(help="Name the run instead of generating an id.")] = None,
    runs_dir: RunsDir = Path("runs"),
) -> None:
    """Start a new run from a playbook."""
    settings = load_config(config or (DEFAULT_CONFIG if DEFAULT_CONFIG.is_file() else None))
    if auto_approve:
        settings = settings.model_copy(update={"auto_approve": True})
    _run(runs_dir, lambda rt: rt.start(playbook, intent, settings, run_id))


@app.command()
def status(run_id: str, runs_dir: RunsDir = Path("runs")) -> None:
    """Show where a run stands."""
    _run(runs_dir, lambda rt: rt.status(run_id))


@app.command()
def approve(
    run_id: str,
    edit: Annotated[Optional[Path], typer.Option(exists=True, dir_okay=False, help="An edited plan or story (YAML or JSON) to use instead.")] = None,
    runs_dir: RunsDir = Path("runs"),
) -> None:
    """Approve the plan or story a run is waiting on, optionally replacing it with an edited file."""
    _run(runs_dir, lambda rt: rt.approve(run_id, edit))


@app.command()
def resume(run_id: str, runs_dir: RunsDir = Path("runs")) -> None:
    """Continue a run that stopped part-way (crash or failed stage) from its last checkpoint."""
    _run(runs_dir, lambda rt: rt.resume(run_id))


@app.command()
def rerun(
    run_id: str,
    from_node: Annotated[str, typer.Option("--from", help="The stage to start again from, e.g. write_story.")],
    intent: Annotated[Optional[Path], typer.Option(exists=True, dir_okay=False, help="A new author's brief to use from here on.")] = None,
    runs_dir: RunsDir = Path("runs"),
) -> None:
    """Run again from a given stage. Earlier stages are not repeated."""
    _run(runs_dir, lambda rt: rt.rerun(run_id, from_node, intent))


def _service():
    """The service handle, built from the environment. Needs the `service` extra installed."""
    load_env()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        from demogen.service.core import Service
        from demogen.service.settings import Settings
    except ImportError as error:
        typer.echo(f"error: the service parts are not installed ({error.name}). Install them with: pip install 'demogen[service]'", err=True)
        raise typer.Exit(1)
    try:
        return Service(Settings.from_env())
    except DemogenError as error:
        typer.echo(f"error: {error}", err=True)
        raise typer.Exit(1)


@app.command()
def migrate() -> None:
    """Service: create the tables the service needs in Postgres. Safe to run again."""
    _service().migrate()
    typer.echo("database ready")


@app.command()
def worker(
    pool: Annotated[list[str], typer.Option(help="A pool to take jobs from: control, browser, gpu or cpu. Repeat for several.")],
    concurrency: Annotated[int, typer.Option(help="Jobs this worker runs at once.")] = 4,
) -> None:
    """Service: run a worker. `control` drives runs; the other pools run their stages."""
    from demogen.service.core import POOLS

    unknown = [name for name in pool if name not in POOLS]
    if unknown:
        typer.echo(f"error: unknown pool {unknown[0]!r}; choose from {', '.join(POOLS)}", err=True)
        raise typer.Exit(1)
    if "control" in pool and len(pool) > 1 and concurrency < 2:
        typer.echo("error: a worker that both drives runs and runs their stages needs --concurrency 2 or more", err=True)
        raise typer.Exit(1)
    _service().run_worker(pool, concurrency)


@app.command()
def serve(host: str = "127.0.0.1", port: int = 8000) -> None:
    """Service: run the HTTP API."""
    import uvicorn

    from demogen.service.api import create_api

    service = _service()
    try:
        with service.opened():
            uvicorn.run(create_api(service), host=host, port=port)
    except DemogenError as error:
        typer.echo(f"error: {error}", err=True)
        raise typer.Exit(1)


if __name__ == "__main__":
    app()

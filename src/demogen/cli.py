from __future__ import annotations

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
        typer.echo(f"status  done: {runs_dir / status.run_id / status.state['report']['path']}")


def _run(runs_dir: Path, action) -> None:
    load_env()
    runtime = Runtime(runs_dir)
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


if __name__ == "__main__":
    app()

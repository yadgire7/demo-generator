from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Annotated, Optional

import typer
import yaml
from pydantic import ValidationError

from demogen import telemetry
from demogen.config import load_config
from demogen.env import load_env
from demogen.errors import DemogenError
from demogen.failures import read_failures
from demogen.graph.runtime import RunStatus, Runtime
from demogen.store.stores import LocalStores
from demogen.ui.local import LocalRunList, name_from_prompt

app = typer.Typer(no_args_is_help=True, add_completion=False, help="Generate narrated product demos from a playbook.")

RunsDir = Annotated[
    Path, typer.Option("--runs-dir", envvar="DEMOGEN_RUNS_DIR", help="Where runs and their checkpoints live.")
]
DEFAULT_CONFIG = Path("demogen.yaml")


def _report(status: RunStatus, runs_dir: Path) -> None:
    record = LocalRunList(runs_dir).get(status.run_id) or {}
    typer.echo(f"run     {status.run_id}")
    typer.echo(f"folder  {runs_dir / status.run_id}")
    if status.phase == "awaiting_approval":
        kind, artifact = status.awaiting["kind"], status.awaiting["artifact"]["path"]
        failure = status.awaiting.get("failure")
        if failure:
            typer.echo(f"status  rehearsal could not do step {failure['step_id']!r}: {failure['reason']}")
            typer.echo(f"        revise the playbook ({runs_dir / status.run_id / artifact}), or approve it as it is to rehearse again")
        else:
            typer.echo(f"status  waiting for you to approve the {kind}: {runs_dir / status.run_id / artifact}")
        typer.echo(f"next    demogen approve {status.run_id}   (add --edit FILE to replace the {kind})")
        if kind == "playbook" and status.state.get("prompt_src"):
            typer.echo(f'        demogen approve {status.run_id} --feedback "what to change"   (to have it redrafted)')
    elif status.phase == "incomplete":
        typer.echo(f"status  stopped before: {', '.join(status.next) or 'the first stage'}")
        if record.get("error"):
            typer.echo(f"error   {record['error']}")
        failed = [f for f in read_failures(LocalStores(runs_dir).for_run(status.run_id)) if f["step_outcome"] == "failed"]
        if failed and failed[-1]["files"].get("screenshot"):
            typer.echo(f"seen    {runs_dir / status.run_id / failed[-1]['files']['screenshot']}   ({failed[-1]['page'].get('title') or failed[-1]['page'].get('url') or 'the page'})")
        typer.echo(f"next    demogen resume {status.run_id}")
    else:
        folder = runs_dir / status.run_id
        final = status.state["final"].get("video")
        typer.echo(f"status  done: {folder / (final or status.state['report'])['path']}")
        report = json.loads((folder / status.state["report"]["path"]).read_text())
        concerns = len(report["audio_warnings"]) + len(report["overrunning_steps"]) + len(report.get("final_check", {}).get("problems", []))
        typer.echo(f"report  {folder / status.state['report']['path']}" + (f"   ({concerns} thing(s) to look at)" if concerns else ""))


def _run(runs_dir: Path, action, run_id: str | None = None) -> None:
    """Do `action(runtime, run)` and report where the run stands. `run` holds the run's id once known.

    Why a run stopped is kept in its folder, so `demogen status` and the app can say later.
    """
    load_env()
    _tracing("cli")
    runtime = Runtime.local(runs_dir)
    runs = LocalRunList(runs_dir)
    run = {"id": run_id}
    try:
        status = action(runtime, run)
        if run["id"] and status.phase != "incomplete":
            runs.set_error(status.run_id, None)
        runs.set_finished(status.run_id, status.phase == "done")
        _report(status, runs_dir)
    except (DemogenError, ValidationError, yaml.YAMLError) as error:
        if run["id"] and runs.get(run["id"]):
            runs.set_error(run["id"], str(error))
        typer.echo(f"error: {error}", err=True)
        raise typer.Exit(1)
    finally:
        runtime.close()


@app.command()
def run(
    playbook: Annotated[Optional[Path], typer.Argument(exists=True, dir_okay=False, help="The playbook to run. Omit it and pass --prompt to have one drafted.")] = None,
    prompt: Annotated[Optional[str], typer.Option(help="Describe the demo in words; a playbook is drafted for you to approve, edit or send back.")] = None,
    url: Annotated[Optional[str], typer.Option(help="With --prompt: the address the demo starts at, if the prompt does not make it clear.")] = None,
    intent: Annotated[Optional[Path], typer.Option(exists=True, dir_okay=False, help="The author's brief (markdown).")] = None,
    config: Annotated[Optional[Path], typer.Option(exists=True, dir_okay=False, help="Defaults to ./demogen.yaml if present.")] = None,
    auto_approve: Annotated[bool, typer.Option("--auto-approve", help="Do not pause for approvals.")] = False,
    run_id: Annotated[Optional[str], typer.Option(help="Name the run instead of generating an id.")] = None,
    runs_dir: RunsDir = Path("runs"),
) -> None:
    """Start a new run from a playbook, or from a prompt describing the demo."""
    settings = load_config(config or (DEFAULT_CONFIG if DEFAULT_CONFIG.is_file() else None))
    if auto_approve:
        settings = settings.model_copy(update={"auto_approve": True})
    def start(runtime: Runtime, run: dict) -> RunStatus:
        run["id"], step = runtime.prepare_start(
            playbook.read_bytes() if playbook else None, intent.read_bytes() if intent else None, settings, run_id,
            playbook.stem if playbook else "demo", prompt=prompt, url=url,
        )  # fmt: skip
        LocalRunList(runs_dir).add(run["id"], playbook.stem if playbook else name_from_prompt(prompt or ""))
        runtime.advance(run["id"], step)
        return runtime.status(run["id"])

    _run(runs_dir, start)


@app.command()
def status(run_id: str, runs_dir: RunsDir = Path("runs")) -> None:
    """Show where a run stands."""
    _run(runs_dir, lambda rt, run: rt.status(run_id))


@app.command()
def approve(
    run_id: str,
    edit: Annotated[Optional[Path], typer.Option(exists=True, dir_okay=False, help="An edited playbook, plan or story (YAML or JSON) to use instead.")] = None,
    feedback: Annotated[Optional[str], typer.Option(help="For a drafted playbook: what to change. It is redrafted and comes back for approval.")] = None,
    runs_dir: RunsDir = Path("runs"),
) -> None:
    """Approve what a run is waiting on, replace it with an edited file, or send a drafted playbook back with feedback."""
    _run(runs_dir, lambda rt, run: rt.approve(run_id, edit, feedback), run_id)


@app.command()
def resume(run_id: str, runs_dir: RunsDir = Path("runs")) -> None:
    """Continue a run that stopped part-way (crash or failed stage) from its last checkpoint."""
    _run(runs_dir, lambda rt, run: rt.resume(run_id), run_id)


@app.command()
def rerun(
    run_id: str,
    from_node: Annotated[str, typer.Option("--from", help="The stage to start again from, e.g. write_story.")],
    intent: Annotated[Optional[Path], typer.Option(exists=True, dir_okay=False, help="A new author's brief to use from here on.")] = None,
    runs_dir: RunsDir = Path("runs"),
) -> None:
    """Run again from a given stage. Earlier stages are not repeated."""
    _run(runs_dir, lambda rt, run: rt.rerun(run_id, from_node, intent), run_id)


@app.command()
def failures(
    run_ids: Annotated[Optional[list[str]], typer.Argument(help="Runs to export. Every run, if none are named.")] = None,
    out: Annotated[Optional[Path], typer.Option("--out", "-o", help="Write to this file instead of the terminal.")] = None,
    runs_dir: RunsDir = Path("runs"),
) -> None:
    """Export fail points as JSON Lines: one record per line, oldest first within each run.

    Each record says what the agent was asked, what it saw and did, what went wrong and whether
    it recovered. Its `files` (screenshot, accessibility snapshots) are relative to its run's folder.
    """
    stores = LocalStores(runs_dir)
    every = sorted(path.name for path in runs_dir.iterdir() if (path / "failures").is_dir()) if runs_dir.is_dir() else []
    names = run_ids or every
    lines = []
    for run_id in names:
        if not stores.exists(run_id):
            raise typer.BadParameter(f"no run named {run_id!r} in {runs_dir}")
        lines += [json.dumps(record, ensure_ascii=False) for record in read_failures(stores.for_run(run_id))]
    text = "".join(line + "\n" for line in lines)
    if out:
        out.write_text(text)
        typer.echo(f"{len(lines)} fail point(s) from {len(names)} run(s) written to {out}", err=True)
    else:
        typer.echo(text, nl=False)


def _tracing(role: str) -> None:
    try:
        telemetry.configure(role)
    except DemogenError as error:
        typer.echo(f"error: {error}", err=True)
        raise typer.Exit(1)


def _service(role: str):
    """The service handle, built from the environment. Needs the `service` extra installed."""
    load_env()
    _tracing(role)
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
    _service("migrate").migrate()
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
    _service("worker").run_worker(pool, concurrency)


@app.command()
def serve(host: str = "127.0.0.1", port: int = 8000) -> None:
    """Service: run the HTTP API."""
    import uvicorn

    from demogen.service.api import create_api

    service = _service("api")
    try:
        with service.opened():
            uvicorn.run(create_api(service, token=service.settings.api_token), host=host, port=port)
    except DemogenError as error:
        typer.echo(f"error: {error}", err=True)
        raise typer.Exit(1)


@app.command()
def ui(
    port: Annotated[int, typer.Option(help="The port to serve the app on, on this machine only.")] = 8765,
    config: Annotated[Optional[Path], typer.Option(exists=True, dir_okay=False, help="Settings for new demos. Defaults to ./demogen.yaml.")] = None,
    open_browser: Annotated[bool, typer.Option("--open/--no-open", help="Open the app in your browser.")] = True,
    runs_dir: RunsDir = Path("runs"),
) -> None:
    """Open the app: start demos, review them and watch them come together, in your browser."""
    import threading
    import webbrowser

    import uvicorn

    from demogen.ui.local import LocalBackend
    from demogen.ui.server import build_app

    load_env()
    _tracing("ui")
    backend = LocalBackend(runs_dir, default_config=config or (DEFAULT_CONFIG if DEFAULT_CONFIG.is_file() else None))
    address = f"http://127.0.0.1:{port}"
    typer.echo(f"demogen is running at {address}   (runs are saved in {runs_dir.resolve()})")
    typer.echo("Press Ctrl+C to stop.")
    if open_browser:
        threading.Timer(1.0, webbrowser.open, args=[address]).start()
    try:
        uvicorn.run(build_app(backend), host="127.0.0.1", port=port, log_level="warning")
    finally:
        backend.close()


@app.command()
def shop(
    port: Annotated[int, typer.Option(help="The port to serve the shop on.")] = 8400,
    host: Annotated[str, typer.Option(help="The address to listen on. Keep 127.0.0.1 unless you need it elsewhere.")] = "127.0.0.1",
) -> None:
    """Run Larkspur Market, a test shop to make demos of, on this machine."""
    import uvicorn

    from demogen.testshop import create_shop
    from demogen.testshop.state import TEST_EMAIL, TEST_PASSWORD

    address = f"http://{host}:{port}"
    typer.echo(f"Larkspur Market is open at {address}")
    typer.echo(f"  test account   {TEST_EMAIL} / {TEST_PASSWORD}")
    typer.echo("  test card      4242 4242 4242 4242, any future expiry, any 3-digit code (4000 0000 0000 0002 is declined)")
    typer.echo(f"  reset          curl -fsS -X POST {address}/__reset   (empties every cart; use as a playbook's setup.seed)")
    typer.echo(f'  make a demo    demogen run --url {address} --prompt "Search for a navy t-shirt, choose size L and add it to the cart"')
    typer.echo("Press Ctrl+C to stop.")
    uvicorn.run(create_shop(), host=host, port=port, log_level="warning")


if __name__ == "__main__":
    app()

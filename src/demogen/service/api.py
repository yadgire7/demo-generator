"""The HTTP API. It prepares changes to runs and hands them to a backend, which does the rest.

The same API serves the service (Postgres, a job queue and worker pools) and the local app
(one machine, one run at a time). Each passes a backend with the small surface below.
"""

from __future__ import annotations

import hmac
import json
import mimetypes
import os
import shutil
import tempfile
from pathlib import Path
from typing import Annotated, Any, Protocol

import yaml
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ValidationError

from demogen import media
from demogen.config import Config, load_config
from demogen.failures import read_failures
from demogen.graph.runtime import RunError, Runtime, Step
from demogen.playbook.loader import parse_playbook
from demogen.ui.local import name_from_prompt
from demogen.ui.review import review as review_for


class Backend(Protocol):
    runtime: Runtime
    runs: Any  # add, get, recent, set_error, set_finished
    default_config: Path | None

    def submit(self, run_id: str, step: Step) -> None: ...
    def busy(self, run_id: str) -> bool: ...
    def describe(self, run_id: str) -> dict[str, Any]: ...
    def delete(self, run_id: str) -> None: ...
    def reveal(self, run_id: str) -> None: ...

# Artifacts the API will not hand out: saved sign-in sessions and internal bookkeeping.
PRIVATE_PREFIXES = ("auth/", ".")


class FeedbackRequest(BaseModel):
    feedback: str


class RerunRequest(BaseModel):
    from_stage: str
    intent: str | None = None


# Pydantic's wording for the commonest mistakes, said the way an author would understand it.
_PLAIN = {
    "missing": "is missing",
    "extra_forbidden": "is not something this file can have (check the spelling)",
    "too_short": "needs at least one entry",
}


def plain_errors(error: ValidationError) -> str:
    """One line per problem, naming where it is: 'step 2 › do: is missing'."""
    lines = []
    for problem in error.errors():
        where, loc = [], list(problem["loc"])
        while loc:
            part = loc.pop(0)
            if part == "steps" and loc and isinstance(loc[0], int):
                where.append(f"step {loc.pop(0) + 1}")
            else:
                where.append(str(part + 1) if isinstance(part, int) else part)
        message = _PLAIN.get(problem["type"], problem["msg"].removeprefix("Value error, "))
        lines.append(f"{' › '.join(where) or 'the file'}: {message}")
    return "\n".join(lines)


def _merged(base: dict[str, Any], changes: dict[str, Any]) -> dict[str, Any]:
    """`base` with `changes` laid over it, nested settings merged rather than replaced."""
    merged = dict(base)
    for key, value in changes.items():
        merged[key] = _merged(merged[key], value) if isinstance(value, dict) and isinstance(merged.get(key), dict) else value
    return merged


def create_api(service: Backend, *, token: str | None, require_token: bool = True) -> FastAPI:
    """The API over a backend. Every request needs `token`, unless `require_token` is off (the local app)."""
    if require_token and not token:
        raise RunError("DEMOGEN_API_TOKEN is not set; the API will not start without one")

    def authorized(request: Request) -> None:
        if not require_token:
            return
        supplied = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        if not hmac.compare_digest(supplied.encode(), token.encode()):
            raise HTTPException(401, "missing or wrong API token", headers={"WWW-Authenticate": "Bearer"})

    api = FastAPI(title="demogen", dependencies=[Depends(authorized)])

    @api.exception_handler(RunError)
    def run_error(request: Request, error: RunError) -> JSONResponse:
        text = str(error)
        code = 404 if text.startswith("no run named") else 409 if "is not waiting" in text or "already exists" in text else 400
        return JSONResponse({"detail": text}, status_code=code)

    @api.exception_handler(ValidationError)
    def invalid(request: Request, error: ValidationError) -> JSONResponse:
        return JSONResponse({"detail": plain_errors(error)}, status_code=422)

    @api.exception_handler(yaml.YAMLError)
    def bad_yaml(request: Request, error: yaml.YAMLError) -> JSONResponse:
        return JSONResponse({"detail": f"not valid YAML: {error}"}, status_code=422)

    def idle(run_id: str) -> None:
        if service.busy(run_id):
            raise HTTPException(409, f"run {run_id} is busy; wait for it to pause or finish")

    def summary(run_id: str) -> dict[str, Any]:
        described = service.describe(run_id)
        state = described.pop("state")
        record = service.runs.get(run_id) or {}
        described["name"] = record.get("name") or run_id
        described["created_at"] = record["created_at"].isoformat() if record.get("created_at") else None
        described["started_from"] = "prompt" if state.get("prompt_src") else "playbook"
        described["target_url"] = state.get("target_url")
        final = state.get("final") or {}
        described["artifacts"] = {
            name: ref["path"]
            for name, ref in (("video", final.get("video")), ("captions", final.get("srt")), ("report", state.get("report")), ("playbook", state.get("playbook_src")), ("plan", state.get("plan")), ("story", state.get("story")))
            if ref
        }
        if described["awaiting"]:
            described["awaiting"] = {"kind": described["awaiting"]["kind"], "artifact": described["awaiting"]["artifact"]["path"]}
        return described

    @api.post("/runs", status_code=202)
    async def start_run(
        playbook: Annotated[UploadFile | None, File()] = None,
        prompt: Annotated[str | None, Form()] = None,
        url: Annotated[str | None, Form()] = None,
        intent: Annotated[UploadFile | None, File()] = None,
        config: Annotated[UploadFile | None, File()] = None,
        options: Annotated[str | None, Form()] = None,
        run_id: Annotated[str | None, Form()] = None,
    ) -> dict[str, Any]:
        """Start a run from a `playbook` file, or from a `prompt` describing the demo, for a playbook to be drafted.

        `config` replaces the default settings entirely; `options` (JSON) changes only the settings it names.
        """
        settings = Config.model_validate(yaml.safe_load(await config.read()) or {}) if config else load_config(service.default_config)
        if options:
            settings = Config.model_validate(_merged(settings.model_dump(mode="json"), json.loads(options)))
        name = (playbook.filename or "run").rsplit(".", 1)[0] if playbook else "demo"
        run_id, step = service.runtime.prepare_start(
            await playbook.read() if playbook else None, await intent.read() if intent else None, settings, run_id, name, prompt=prompt, url=url
        )
        service.runs.add(run_id, name if playbook else name_from_prompt(prompt or ""))
        service.submit(run_id, step)
        return summary(run_id)

    @api.get("/runs")
    def list_runs(limit: int = 50) -> list[dict[str, Any]]:
        listed = []
        for record in service.runs.recent(limit):
            entry = {
                "run_id": record["run_id"],
                "name": record["name"],
                "created_at": record["created_at"].isoformat(),
                "finished": record["finished_at"] is not None,
                "failed": record["error"] is not None,
            }
            try:
                described = service.describe(record["run_id"])
                entry["phase"] = described["phase"]
                entry["next"] = described["next"]
                entry["awaiting"] = (described["awaiting"] or {}).get("kind")
            except RunError:
                entry["phase"], entry["next"], entry["awaiting"] = "unknown", [], None
            listed.append(entry)
        return listed

    @api.get("/runs/{run_id}")
    def get_run(run_id: str) -> dict[str, Any]:
        return summary(run_id)

    @api.post("/runs/{run_id}/approve", status_code=202)
    async def approve(run_id: str, request: Request) -> dict[str, Any]:
        """Approve what the run is waiting on. A request body, if any, is the edited playbook, plan or story."""
        idle(run_id)
        body = (await request.body()).decode()
        service.submit(run_id, service.runtime.prepare_approval(run_id, body if body.strip() else None))
        return summary(run_id)

    @api.post("/runs/{run_id}/feedback", status_code=202)
    def feedback(run_id: str, body: FeedbackRequest) -> dict[str, Any]:
        """Send a drafted playbook back with changes to make. It is redrafted and waits for approval again."""
        idle(run_id)
        service.submit(run_id, service.runtime.prepare_approval(run_id, feedback=body.feedback))
        return summary(run_id)

    @api.post("/runs/{run_id}/resume", status_code=202)
    def resume(run_id: str) -> dict[str, Any]:
        idle(run_id)
        step = service.runtime.prepare_resume(run_id)
        if step is None:
            raise HTTPException(409, f"run {run_id} has nothing to resume")
        service.submit(run_id, step)
        return summary(run_id)

    @api.post("/runs/{run_id}/rerun", status_code=202)
    def rerun(run_id: str, body: RerunRequest) -> dict[str, Any]:
        idle(run_id)
        service.submit(run_id, service.runtime.prepare_rerun(run_id, body.from_stage, body.intent))
        service.runs.set_finished(run_id, False)
        return summary(run_id)

    @api.get("/runs/{run_id}/review")
    def get_review(run_id: str) -> dict[str, Any]:
        """What the screen for the run's current pause shows."""
        try:
            return review_for(service.runtime, run_id)
        except LookupError as error:
            raise HTTPException(409, str(error)) from error

    @api.get("/runs/{run_id}/failures")
    def get_failures(run_id: str) -> list[dict[str, Any]]:
        """The run's fail points, oldest first: each mistake or breakage, with the page as it was."""
        return read_failures(service.runtime.context(run_id).store)

    @api.delete("/runs/{run_id}", status_code=204)
    def delete_run(run_id: str) -> Response:
        """Remove the run, its checkpoints and every artifact it made."""
        service.delete(run_id)
        return Response(status_code=204)

    @api.post("/runs/{run_id}/reveal", status_code=204)
    def reveal(run_id: str) -> Response:
        """Show the run's folder in the file manager. Only the local app can."""
        service.reveal(run_id)
        return Response(status_code=204)

    @api.get("/runs/{run_id}/poster")
    def poster(run_id: str) -> Response:
        """A still for the run: from the finished video, or else its first keyframe."""
        status = service.runtime.status(run_id)
        store = service.runtime.context(run_id).store
        final = (status.state.get("final") or {}).get("video")
        if final:
            try:
                return FileResponse(store.local_path({"path": "poster.jpg", "sha256": store.sha256("poster.jpg")}), media_type="image/jpeg")
            except FileNotFoundError:
                with tempfile.TemporaryDirectory() as scratch:
                    still = Path(scratch) / "poster.jpg"
                    media.run("ffmpeg", "-loglevel", "error", "-ss", "1", "-i", str(store.local_path(final)), "-frames:v", "1", "-vf", "scale=640:-2", str(still))
                    ref = store.put_file("poster.jpg", still)
                return FileResponse(store.local_path(ref), media_type="image/jpeg")
        if status.state.get("keyframes"):
            first = store.get_json(status.state["keyframes"])["steps"][0]["before"]["image"]
            return FileResponse(store.local_path(first), media_type="image/png")
        raise HTTPException(404, f"run {run_id} has no picture yet")

    @api.post("/playbooks/check")
    async def check_playbook(playbook: Annotated[UploadFile, File()]) -> dict[str, Any]:
        """Check an uploaded playbook without starting a run."""
        parsed = parse_playbook((await playbook.read()).decode())
        return {"name": parsed.name, "base_url": parsed.target.base_url, "steps": [{"id": step.id, "do": step.do} for step in parsed.steps]}

    @api.get("/settings/health")
    def health_check() -> dict[str, Any]:
        """Whether this machine is ready to make a demo, and what to fix if not."""
        config_path = service.default_config
        settings = load_config(config_path if config_path and config_path.is_file() else None)
        problems = []
        if not config_path or not config_path.is_file():
            problems.append("No demogen.yaml found. Copy demogen.example.yaml to demogen.yaml and choose your models.")
        missing = [role for role in ("planner", "vision", "writer") if role not in settings.models]
        if missing and config_path and config_path.is_file():
            problems.append(f"demogen.yaml has no model for: {', '.join(missing)}.")
        if any(model.provider == "anthropic" for model in settings.models.values()) and not os.environ.get("ANTHROPIC_API_KEY"):
            problems.append("ANTHROPIC_API_KEY is not set. Put it in a .env file next to demogen.yaml.")
        if not shutil.which("ffmpeg"):
            problems.append("ffmpeg is not installed. On a Mac: brew install ffmpeg")
        runs_dir = getattr(service, "runs_dir", None)
        return {
            "ready": not problems,
            "problems": problems,
            "config": str(config_path) if config_path else None,
            "runs_dir": str(runs_dir) if runs_dir else None,
            # What a new demo gets unless the author changes it.
            "defaults": {
                "auto_approve": settings.auto_approve,
                "render": {"height": settings.render.height, "zoom": settings.render.zoom, "captions": settings.render.captions},
                "voice": {"tts": {"voice": settings.voice.tts.voice}},
            },
        }

    @api.get("/runs/{run_id}/artifacts/{path:path}")
    def artifact(run_id: str, path: str) -> Response:
        store = service.runtime.context(run_id).store
        if path.startswith(PRIVATE_PREFIXES) or "/." in path:
            raise HTTPException(403, "this artifact is not available through the API")
        try:
            ref = {"path": path, "sha256": store.sha256(path)}
            return FileResponse(store.local_path(ref), media_type=mimetypes.guess_type(path)[0] or "application/octet-stream", filename=path.rsplit("/", 1)[-1])
        except (FileNotFoundError, ValueError) as error:
            raise HTTPException(404, f"run {run_id} has no artifact {path}") from error

    health = FastAPI()

    @health.get("/")
    def healthy() -> dict[str, str]:
        return {"status": "ok"}

    api.mount("/healthz", health)  # mounted apart, so it needs no token
    return api

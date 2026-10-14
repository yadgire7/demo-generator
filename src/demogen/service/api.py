"""The HTTP API. It prepares changes to runs and queues them; workers do the rest."""

from __future__ import annotations

import hmac
import mimetypes
from typing import Annotated, Any

import yaml
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ValidationError

from demogen.config import Config, load_config
from demogen.graph.runtime import RunError
from demogen.service.core import Service

# Artifacts the API will not hand out: saved sign-in sessions and internal bookkeeping.
PRIVATE_PREFIXES = ("auth/", ".")


class FeedbackRequest(BaseModel):
    feedback: str


class RerunRequest(BaseModel):
    from_stage: str
    intent: str | None = None


def create_api(service: Service) -> FastAPI:
    token = service.settings.api_token
    if not token:
        raise RunError("DEMOGEN_API_TOKEN is not set; the API will not start without one")

    def authorized(request: Request) -> None:
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
        return JSONResponse({"detail": str(error)}, status_code=422)

    @api.exception_handler(yaml.YAMLError)
    def bad_yaml(request: Request, error: yaml.YAMLError) -> JSONResponse:
        return JSONResponse({"detail": f"not valid YAML: {error}"}, status_code=422)

    def idle(run_id: str) -> None:
        if service.busy(run_id):
            raise HTTPException(409, f"run {run_id} is busy; wait for it to pause or finish")

    def summary(run_id: str) -> dict[str, Any]:
        described = service.describe(run_id)
        state = described.pop("state")
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
        run_id: Annotated[str | None, Form()] = None,
    ) -> dict[str, Any]:
        """Start a run from a `playbook` file, or from a `prompt` describing the demo, for a playbook to be drafted."""
        settings = Config.model_validate(yaml.safe_load(await config.read()) or {}) if config else load_config(service.settings.default_config)
        name = (playbook.filename or "run").rsplit(".", 1)[0] if playbook else "demo"
        run_id, step = service.runtime.prepare_start(
            await playbook.read() if playbook else None, await intent.read() if intent else None, settings, run_id, name, prompt=prompt, url=url
        )
        service.runs.add(run_id, name)
        service.submit(run_id, step)
        return summary(run_id)

    @api.get("/runs")
    def list_runs(limit: int = 50) -> list[dict[str, Any]]:
        return [
            {"run_id": r["run_id"], "name": r["name"], "created_at": r["created_at"].isoformat(), "finished": r["finished_at"] is not None, "failed": r["error"] is not None}
            for r in service.runs.recent(limit)
        ]

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

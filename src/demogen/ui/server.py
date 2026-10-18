"""The local app's web server: the interface, the API under /api, and the sample store."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles

from demogen.service.api import create_api
from demogen.ui.local import LocalBackend

STATIC = Path(__file__).parent / "static"
# The sample store, when demogen runs from a checkout of its repository.
SAMPLE_STORE = Path(__file__).resolve().parents[3] / "examples" / "shop"
LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]"}


def build_app(backend: LocalBackend) -> FastAPI:
    app = FastAPI(title="demogen", docs_url=None, redoc_url=None)

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        # The server listens on this machine only. A request naming another host is a web page
        # elsewhere trying to reach it through DNS rebinding: refuse it.
        host = (request.headers.get("host") or "").rsplit(":", 1)[0] if not (request.headers.get("host") or "").startswith("[") else (request.headers.get("host") or "").split("]")[0] + "]"
        if host not in LOCAL_HOSTS:
            return PlainTextResponse("demogen only answers requests addressed to this machine", status_code=403)
        return await call_next(request)

    app.mount("/api", create_api(backend, token=None, require_token=False))
    if SAMPLE_STORE.is_dir():
        app.mount("/examples/shop", StaticFiles(directory=SAMPLE_STORE, html=True), name="sample-store")
    app.mount("/", StaticFiles(directory=STATIC, html=True), name="app")
    return app

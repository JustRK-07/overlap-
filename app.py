#!/usr/bin/env python3
"""overlay-app — FastAPI entrypoint.

Single-file FastAPI app that:
  - serves the single-page UI at GET /
  - accepts 2 DWG/DXF uploads at POST /api/v1/overlay
  - drives the pipeline through 9 stages via a worker thread
  - polls job state via GET /api/v1/overlay/{id}/status
  - accepts user grid convention at POST /api/v1/overlay/{id}/axes
  - streams the final DXF via GET /api/v1/overlay/{id}/download

Run:  python3 -m uvicorn app:app --host 0.0.0.0 --port 8000
Or:   ./run.sh
"""

from __future__ import annotations

import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

HERE = Path(__file__).resolve().parent
STATIC_DIR = HERE / "static"
JOBS_DIR = HERE / "jobs"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown. We just ensure jobs/ exists here."""
    JOBS_DIR.mkdir(exist_ok=True)
    yield


app = FastAPI(
    title="ISGEC Overlay",
    description="Superimpose two CAD drawings in your browser",
    version="0.1.0",
    lifespan=lifespan,
)

# Serve static files (CSS, JS, images)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", response_class=HTMLResponse)
async def index():
    """Serve the single-page UI."""
    return (STATIC_DIR / "index.html").read_text()


@app.get("/healthz")
async def health():
    """Liveness probe — used by smoke test."""
    return {"ok": True, "version": "0.1.0"}


# Wire up the API router (upload + status + job state)
from routers import api as api_router
app.include_router(api_router.router)

"""FastAPI entry point."""
from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from typing import AsyncIterator

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from mapng_ai import config
from mapng_ai.library_builder import build_library
from mapng_ai.library_builder.runner import library_status
from mapng_ai.pipeline import BBox, JobContext, run_pipeline


# Load .env (project root) before anything reads env vars
_dotenv = config.ROOT / ".env"
if _dotenv.exists():
    for line in _dotenv.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


config.ensure_runtime_dirs()

app = FastAPI(title="MapNG-AI", version="0.1.0")

# Cache-bust static asset URLs every time the server starts, so the browser
# never serves stale UI code after we restart.
BUILD_ID = str(int(time.time()))


class _NoCacheStatic(StaticFiles):
    async def get_response(self, path: str, scope):
        resp = await super().get_response(path, scope)
        resp.headers["Cache-Control"] = "no-cache, must-revalidate"
        return resp


app.mount("/static", _NoCacheStatic(directory=str(config.STATIC_DIR)), name="static")


# ---------------------------------------------------------------------------
# In-memory job registry
# ---------------------------------------------------------------------------
class _Job:
    def __init__(self, ctx: JobContext) -> None:
        self.ctx = ctx
        self.queue: asyncio.Queue[tuple[str, dict] | None] = asyncio.Queue()
        self.done = False

    async def emit(self, event: str, data: dict) -> None:
        await self.queue.put((event, data))

    async def close(self) -> None:
        self.done = True
        await self.queue.put(None)


_jobs: dict[str, _Job] = {}


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
class GenerateRequest(BaseModel):
    west: float = Field(..., ge=-180, le=180)
    south: float = Field(..., ge=-90, le=90)
    east: float = Field(..., ge=-180, le=180)
    north: float = Field(..., ge=-90, le=90)


class GenerateResponse(BaseModel):
    job_id: str


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.get("/")
async def index() -> HTMLResponse:
    html = (config.TEMPLATES_DIR / "index.html").read_text(encoding="utf-8")
    html = html.replace("__BUILD__", BUILD_ID)
    return HTMLResponse(html, headers={"Cache-Control": "no-cache, must-revalidate"})


@app.get("/api/health")
async def health() -> dict:
    return {"ok": True, "version": app.version}


@app.post("/api/generate", response_model=GenerateResponse)
async def generate(req: GenerateRequest) -> GenerateResponse:
    if req.east <= req.west or req.north <= req.south:
        raise HTTPException(400, "Invalid bbox: east must be > west, north must be > south")

    job_id = uuid.uuid4().hex[:12]
    out_dir = config.OUTPUT_DIR / job_id
    ctx = JobContext(
        job_id=job_id,
        bbox_ll=BBox(req.west, req.south, req.east, req.north),
        out_dir=out_dir,
    )
    job = _Job(ctx)
    _jobs[job_id] = job
    asyncio.create_task(_run_job(job))
    return GenerateResponse(job_id=job_id)


async def _run_job(job: _Job) -> None:
    try:
        await run_pipeline(job.ctx, job.emit)
    except Exception as exc:
        await job.emit("pipeline:error", {"message": f"{type(exc).__name__}: {exc}"})
    finally:
        await job.close()


@app.get("/api/jobs/{job_id}/events")
async def stream_events(job_id: str) -> EventSourceResponse:
    job = _jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "Unknown job_id")

    async def event_gen() -> AsyncIterator[dict]:
        while True:
            item = await job.queue.get()
            if item is None:
                break
            event, data = item
            yield {"event": event, "data": json.dumps(data)}

    return EventSourceResponse(event_gen())


# ---------------------------------------------------------------------------
# Library batch builder
# ---------------------------------------------------------------------------
_library_jobs: dict[str, _Job] = {}


@app.get("/api/library/status")
async def get_library_status() -> dict:
    return library_status()


class LibraryBuildRequest(BaseModel):
    categories: list[str] | None = None     # None = all


@app.post("/api/library/build")
async def post_library_build(req: LibraryBuildRequest) -> dict:
    job_id = uuid.uuid4().hex[:12]
    queue: asyncio.Queue[tuple[str, dict] | None] = asyncio.Queue()

    class _LibJob:
        def __init__(self):
            self.queue = queue
            self.done = False
            self.ctx = type("ctx", (), {"out_dir": config.OUTPUT_DIR})()  # dummy

        async def emit(self, event: str, data: dict) -> None:
            await self.queue.put((event, data))

        async def close(self) -> None:
            self.done = True
            await self.queue.put(None)

    job = _LibJob()
    _library_jobs[job_id] = job

    async def _run():
        try:
            await build_library(categories=req.categories, emit=job.emit)
        except Exception as exc:
            await job.emit("batch:error", {"message": f"{type(exc).__name__}: {exc}"})
        finally:
            await job.close()

    asyncio.create_task(_run())
    return {"job_id": job_id}


@app.get("/api/library/jobs/{job_id}/events")
async def stream_library_events(job_id: str) -> EventSourceResponse:
    job = _library_jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "Unknown library job_id")

    async def event_gen() -> AsyncIterator[dict]:
        while True:
            item = await job.queue.get()
            if item is None:
                break
            event, data = item
            yield {"event": event, "data": json.dumps(data)}

    return EventSourceResponse(event_gen())


@app.get("/api/jobs/{job_id}/files/{path:path}")
async def get_artifact(job_id: str, path: str) -> FileResponse:
    """Serve a generated artefact (heightmap PNG, GeoTIFF, level zip, …)."""
    if ".." in path or path.startswith(("/", "\\")):
        raise HTTPException(400, "Invalid path")
    job = _jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "Unknown job_id")
    full = (job.ctx.out_dir / path).resolve()
    if not str(full).startswith(str(job.ctx.out_dir.resolve())) or not full.exists():
        raise HTTPException(404, "Artifact not found")
    return FileResponse(full)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def run() -> None:
    uvicorn.run("mapng_ai.app:app", host=config.HOST, port=config.PORT, reload=False)


if __name__ == "__main__":
    run()

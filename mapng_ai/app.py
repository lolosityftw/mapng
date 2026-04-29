"""FastAPI entry point — Phase 0 skeleton."""
from __future__ import annotations

import asyncio
import json
import uuid
from typing import AsyncIterator

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from mapng_ai import config
from mapng_ai.pipeline import BBox, run_pipeline


config.ensure_runtime_dirs()

app = FastAPI(title="MapNG-AI", version="0.1.0")
app.mount("/static", StaticFiles(directory=str(config.STATIC_DIR)), name="static")


# ---------------------------------------------------------------------------
# In-memory job registry
# ---------------------------------------------------------------------------
class _JobChannel:
    """Single-consumer event channel for one pipeline run."""

    def __init__(self) -> None:
        self.queue: asyncio.Queue[tuple[str, dict] | None] = asyncio.Queue()
        self.done = False

    async def emit(self, event: str, data: dict) -> None:
        await self.queue.put((event, data))

    async def close(self) -> None:
        self.done = True
        await self.queue.put(None)


_jobs: dict[str, _JobChannel] = {}


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
async def index() -> FileResponse:
    return FileResponse(config.TEMPLATES_DIR / "index.html")


@app.get("/api/health")
async def health() -> dict:
    return {"ok": True, "version": app.version}


@app.post("/api/generate", response_model=GenerateResponse)
async def generate(req: GenerateRequest) -> GenerateResponse:
    if req.east <= req.west or req.north <= req.south:
        raise HTTPException(400, "Invalid bbox: east must be > west, north must be > south")

    job_id = uuid.uuid4().hex[:12]
    channel = _JobChannel()
    _jobs[job_id] = channel

    bbox = BBox(req.west, req.south, req.east, req.north)
    asyncio.create_task(_run_job(channel, bbox))
    return GenerateResponse(job_id=job_id)


async def _run_job(channel: _JobChannel, bbox: BBox) -> None:
    try:
        await run_pipeline(bbox, channel.emit)
    except Exception as exc:  # pragma: no cover  (defensive — real errors come later)
        await channel.emit("pipeline:error", {"message": str(exc)})
    finally:
        await channel.close()


@app.get("/api/jobs/{job_id}/events")
async def stream_events(job_id: str) -> EventSourceResponse:
    channel = _jobs.get(job_id)
    if channel is None:
        raise HTTPException(404, "Unknown job_id")

    async def event_gen() -> AsyncIterator[dict]:
        while True:
            item = await channel.queue.get()
            if item is None:
                break
            event, data = item
            yield {"event": event, "data": json.dumps(data)}

    return EventSourceResponse(event_gen())


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def run() -> None:
    uvicorn.run("mapng_ai.app:app", host=config.HOST, port=config.PORT, reload=False)


if __name__ == "__main__":
    run()

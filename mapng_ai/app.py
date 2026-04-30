"""FastAPI entry point."""
from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from pathlib import Path
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


@app.get("/library")
async def library_page() -> HTMLResponse:
    html = (config.TEMPLATES_DIR / "library.html").read_text(encoding="utf-8")
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


@app.get("/api/library/catalogue")
async def get_library_catalogue() -> dict:
    """Full catalogue + per-entry built/missing state, for the UI to render
    upfront so the panel never looks empty."""
    from mapng_ai.library_builder import CATALOGUE
    from mapng_ai.library_builder.runner import target_glb
    out = []
    for e in CATALOGUE:
        glb = target_glb(e)
        out.append({
            "slug": e.slug,
            "category": e.category,
            "type": e.type,
            "prompt": e.prompt,
            "footprint_m": list(e.footprint_m),
            "levels": e.levels,
            "built": glb.exists() and glb.stat().st_size > 0,
            "size_bytes": glb.stat().st_size if glb.exists() else 0,
        })
    return {"entries": out}


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


class SingleBuildRequest(BaseModel):
    slug: str
    force: bool = False     # if True, regenerate even if cached


@app.post("/api/library/build/single")
async def build_single_entry(req: SingleBuildRequest) -> dict:
    """Generate (or regenerate) one specific catalogue entry."""
    from mapng_ai.assets.meshy import MeshyEngine
    from mapng_ai.library_builder import CATALOGUE
    from mapng_ai.library_builder.runner import target_glb, manifest_path, target_dir

    entry = next((e for e in CATALOGUE if e.slug == req.slug), None)
    if entry is None:
        raise HTTPException(404, f"unknown slug: {req.slug}")

    glb = target_glb(entry)
    if req.force and glb.exists():
        glb.unlink()

    job_id = uuid.uuid4().hex[:12]
    queue: asyncio.Queue[tuple[str, dict] | None] = asyncio.Queue()

    class _LibJob:
        def __init__(self):
            self.queue = queue
            self.done = False

        async def emit(self, event: str, data: dict) -> None:
            await self.queue.put((event, data))

        async def close(self) -> None:
            self.done = True
            await self.queue.put(None)

    job = _LibJob()
    _library_jobs[job_id] = job

    async def _run():
        try:
            from mapng_ai.library_builder.runner import _gen_one, LibraryProgress
            engine = MeshyEngine()
            if not engine.configured:
                await job.emit("batch:error", {"message": "Meshy not configured"})
                return
            progress = LibraryProgress(total=1, completed=0, skipped=0, failed=0, in_progress=[])
            await job.emit("batch:start", {
                "total": 1, "categories": [entry.category],
                "concurrency": engine.concurrency, "rps": engine.rps,
                "texture": engine.texture,
            })
            sem = asyncio.Semaphore(1)
            await _gen_one(entry, engine, sem, progress, job.emit)
            await job.emit("batch:done", {
                "total": 1, "completed": progress.completed,
                "skipped": progress.skipped, "failed": progress.failed,
            })
        except Exception as exc:
            await job.emit("batch:error", {"message": f"{type(exc).__name__}: {exc}"})
        finally:
            await job.close()

    asyncio.create_task(_run())
    return {"job_id": job_id}


@app.delete("/api/library/entries/{slug}")
async def delete_entry(slug: str) -> dict:
    """Drop a cached GLB so the next build regenerates it."""
    from mapng_ai.library_builder import CATALOGUE
    from mapng_ai.library_builder.runner import target_glb, manifest_path

    entry = next((e for e in CATALOGUE if e.slug == slug), None)
    if entry is None:
        raise HTTPException(404, f"unknown slug: {slug}")

    glb = target_glb(entry)
    deleted = False
    if glb.exists():
        glb.unlink()
        deleted = True

    # Update manifest.json
    mp = manifest_path(entry)
    if mp.exists():
        try:
            data = json.loads(mp.read_text(encoding="utf-8"))
            data.pop(glb.name, None)
            mp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        except Exception:
            pass
    return {"deleted": deleted, "slug": slug}


@app.get("/api/library/entries/{slug}/glb")
async def get_entry_glb(slug: str, quality: str = "original") -> FileResponse:
    """Serve a cached GLB at the requested quality so /library can preview
    each variant. `quality` defaults to 'original'."""
    from mapng_ai.library_builder import CATALOGUE
    from mapng_ai.library_builder.optimise import QUALITY_PRESETS, optimise
    from mapng_ai.library_builder.runner import target_glb

    entry = next((e for e in CATALOGUE if e.slug == slug), None)
    if entry is None:
        raise HTTPException(404, f"unknown slug: {slug}")
    if quality not in QUALITY_PRESETS:
        raise HTTPException(400, f"unknown quality: {quality}")
    glb = target_glb(entry)
    if not glb.exists():
        raise HTTPException(404, f"{slug} not yet generated")
    served = await asyncio.to_thread(optimise, glb, quality)
    return FileResponse(
        served,
        media_type="model/gltf-binary",
        headers={"Cache-Control": "public, max-age=3600"},
    )


# ---- Library asset serving (used by /preview) -------------------------------
@app.get("/api/asset")
async def get_asset_by_relpath(path: str, quality: str | None = None) -> FileResponse:
    """Serve a GLB/DAE/PNG from `assets/` by its in-zip relative path.

    `quality` triggers texture downscaling + decimation on GLBs; cached per
    quality on disk. When no quality is given, falls back to the library-wide
    active quality (settable via /api/library/active-quality, defaulting to
    'original' if never set).
    """
    from mapng_ai.library_builder.optimise import (
        optimise, QUALITY_PRESETS, get_active_quality,
    )

    if ".." in path or path.startswith(("/", "\\")):
        raise HTTPException(400, "Invalid path")
    if quality is None:
        quality = get_active_quality()
    if quality not in QUALITY_PRESETS:
        raise HTTPException(400, f"Unknown quality: {quality}")

    fs: Path | None = None
    if path.startswith("art/shapes/buildings_lib/"):
        tail = path[len("art/shapes/buildings_lib/"):]
        fs = config.ROOT / "assets" / "buildings" / tail
    elif path.startswith("art/shapes/trees_lib/"):
        tail = path[len("art/shapes/trees_lib/"):]
        fs = config.ROOT / "assets" / "trees" / tail
    elif path.startswith("art/shapes/vehicles_lib/"):
        tail = path[len("art/shapes/vehicles_lib/"):]
        fs = config.ROOT / "assets" / "vehicles" / tail
    else:
        raise HTTPException(404, f"Path not in a known asset namespace: {path}")

    fs = fs.resolve()
    root = (config.ROOT / "assets").resolve()
    if not str(fs).startswith(str(root)) or not fs.exists():
        raise HTTPException(404, f"Asset not found: {path}")

    # GLBs honour the quality query param — DAEs/PNGs serve as-is
    if fs.suffix.lower() == ".glb":
        fs = await asyncio.to_thread(optimise, fs, quality)

    media = {".glb": "model/gltf-binary", ".gltf": "model/gltf+json", ".dae": "model/vnd.collada+xml"}
    return FileResponse(
        fs,
        media_type=media.get(fs.suffix.lower(), "application/octet-stream"),
        headers={"Cache-Control": "public, max-age=3600"},
    )


@app.get("/api/library/entries/{slug}/stats")
async def get_entry_stats(slug: str) -> dict:
    """Per-entry mesh stats (triangles, texture dims, file size at each quality)."""
    from mapng_ai.library_builder import CATALOGUE
    from mapng_ai.library_builder.optimise import stats_for_all_qualities
    from mapng_ai.library_builder.runner import target_glb

    entry = next((e for e in CATALOGUE if e.slug == slug), None)
    if entry is None:
        raise HTTPException(404, f"unknown slug: {slug}")
    glb = target_glb(entry)
    if not glb.exists():
        return {"slug": slug, "exists": False}
    qualities = await asyncio.to_thread(stats_for_all_qualities, glb)
    return {"slug": slug, "exists": True, "qualities": qualities}


# ---- Active quality (persisted; library owns it, main pipeline reads it) ----
class ActiveQualityRequest(BaseModel):
    quality: str


@app.get("/api/library/active-quality")
async def get_active_quality_api() -> dict:
    from mapng_ai.library_builder.optimise import (
        QUALITY_PRESETS, get_active_quality,
    )
    q = get_active_quality()
    return {"quality": q, "available": list(QUALITY_PRESETS.keys())}


@app.post("/api/library/active-quality")
async def set_active_quality_api(req: ActiveQualityRequest) -> dict:
    from mapng_ai.library_builder.optimise import set_active_quality
    try:
        q = set_active_quality(req.quality)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"quality": q}


# ---- Pre-bake quality variants ----------------------------------------------
class OptimiseAllRequest(BaseModel):
    qualities: list[str] | None = None    # None = use active quality only


@app.post("/api/library/optimise/all")
async def post_optimise_all(req: OptimiseAllRequest = OptimiseAllRequest()) -> dict:
    """Pre-bake every catalogue entry at the requested qualities, in parallel.

    Body: { "qualities": ["5k"] }                # only one
           { "qualities": ["1.5k","5k","10k"] }  # subset
           { "qualities": null }                 # use active quality only
    """
    from mapng_ai.library_builder import CATALOGUE
    from mapng_ai.library_builder.optimise import (
        QUALITY_PRESETS, get_active_quality, optimise,
    )
    from mapng_ai.library_builder.runner import target_glb

    job_id = uuid.uuid4().hex[:12]
    queue: asyncio.Queue[tuple[str, dict] | None] = asyncio.Queue()

    class _LibJob:
        def __init__(self):
            self.queue = queue
        async def emit(self, event, data):
            await self.queue.put((event, data))
        async def close(self):
            await self.queue.put(None)

    job = _LibJob()
    _library_jobs[job_id] = job

    if req.qualities is None:
        qualities = [get_active_quality()]
    else:
        qualities = req.qualities
    # Validate + drop 'original' (no rewrite needed)
    qualities = [q for q in qualities if q in QUALITY_PRESETS and q != "original"]
    if not qualities:
        raise HTTPException(400, "no valid qualities to bake (note: 'original' is a no-op)")

    async def _bake_one(slug, q, src):
        try:
            await asyncio.to_thread(optimise, src, q)
            await job.emit("variant:done", {"slug": slug, "quality": q})
        except Exception as exc:
            await job.emit("variant:fail", {"slug": slug, "quality": q, "msg": str(exc)})

    async def _run():
        try:
            tasks = []
            total = 0
            for entry in CATALOGUE:
                src = target_glb(entry)
                if not src.exists():
                    continue
                for q in qualities:
                    tasks.append(_bake_one(entry.slug, q, src))
                    total += 1
            await job.emit("bake:start", {"total": total, "qualities": qualities})
            sem = asyncio.Semaphore(2)        # decimation is CPU-bound — keep it modest
            async def _gated(t):
                async with sem:
                    await t
            await asyncio.gather(*(_gated(t) for t in tasks), return_exceptions=True)
            await job.emit("bake:done", {})
        except Exception as exc:
            await job.emit("bake:error", {"message": f"{type(exc).__name__}: {exc}"})
        finally:
            await job.close()

    asyncio.create_task(_run())
    return {"job_id": job_id, "qualities": qualities}


@app.post("/api/library/optimise/{slug}")
async def post_optimise_one(slug: str) -> dict:
    """Pre-bake one entry at all qualities."""
    from mapng_ai.library_builder import CATALOGUE
    from mapng_ai.library_builder.optimise import optimise
    from mapng_ai.library_builder.runner import target_glb

    entry = next((e for e in CATALOGUE if e.slug == slug), None)
    if entry is None:
        raise HTTPException(404, f"unknown slug: {slug}")
    src = target_glb(entry)
    if not src.exists():
        raise HTTPException(400, f"{slug} not yet generated")

    job_id = uuid.uuid4().hex[:12]
    queue: asyncio.Queue = asyncio.Queue()
    class _LJ:
        def __init__(self): self.queue = queue
        async def emit(self, e, d): await self.queue.put((e, d))
        async def close(self): await self.queue.put(None)
    job = _LJ()
    _library_jobs[job_id] = job

    async def _run():
        qualities = ["100k", "50k", "10k", "5k", "1.5k"]
        await job.emit("bake:start", {"total": len(qualities), "qualities": qualities})
        for q in qualities:
            try:
                await asyncio.to_thread(optimise, src, q)
                await job.emit("variant:done", {"slug": slug, "quality": q})
            except Exception as exc:
                await job.emit("variant:fail", {"slug": slug, "quality": q, "msg": str(exc)})
        await job.emit("bake:done", {})
        await job.close()

    asyncio.create_task(_run())
    return {"job_id": job_id}


# ---- Terrain PBR pack -------------------------------------------------------
@app.get("/api/library/terrain-pack/status")
async def get_terrain_pack_status() -> dict:
    from mapng_ai.library_builder.terrain_pack import pack_status
    return pack_status()


@app.post("/api/library/terrain-pack/download")
async def post_terrain_pack_download() -> dict:
    """Kick a Poly Haven download for all 8 land cover classes."""
    from mapng_ai.library_builder.terrain_pack import download_terrain_pack

    job_id = uuid.uuid4().hex[:12]
    queue: asyncio.Queue[tuple[str, dict] | None] = asyncio.Queue()

    class _LibJob:
        def __init__(self):
            self.queue = queue
            self.done = False

        async def emit(self, event: str, data: dict) -> None:
            await self.queue.put((event, data))

        async def close(self) -> None:
            self.done = True
            await self.queue.put(None)

    job = _LibJob()
    _library_jobs[job_id] = job

    async def _run():
        try:
            await download_terrain_pack(emit=job.emit)
        except Exception as exc:
            await job.emit("pack:error", {"message": f"{type(exc).__name__}: {exc}"})
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

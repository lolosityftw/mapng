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
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from mapng_ai import config
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


# Auto-prune old job output dirs at boot. Keep the 12 most-recent so
# refreshed pages still resolve their artifact URLs.
def _prune_output_dirs(keep: int = 12) -> None:
    try:
        dirs = [d for d in config.OUTPUT_DIR.iterdir() if d.is_dir()]
        dirs.sort(key=lambda d: d.stat().st_mtime, reverse=True)
        import shutil
        for d in dirs[keep:]:
            try:
                shutil.rmtree(d, ignore_errors=True)
            except Exception:
                pass
    except Exception:
        pass


_prune_output_dirs()

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
    # Optional polygon vertices in [lon, lat] order — the user's drawn
    # shape, ANY shape (not just a rectangle). When set, features
    # outside the polygon get clipped before the BeamNG export.
    polygon: list[list[float]] | None = None
    # Optional explicit terrain side length in metres. None = use the
    # larger of the bbox's projected width/height (clamped 500..8000).
    size_m: float | None = Field(None, ge=500, le=8000)
    # Optional Esri imagery zoom. None = use the source default (18).
    imagery_zoom: int | None = Field(None, ge=14, le=20)


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


@app.get("/library/textures")
async def textures_page() -> HTMLResponse:
    html = (config.TEMPLATES_DIR / "textures.html").read_text(encoding="utf-8")
    html = html.replace("__BUILD__", BUILD_ID)
    return HTMLResponse(html, headers={"Cache-Control": "no-cache, must-revalidate"})


# ---- Ground texture studio --------------------------------------------------
@app.get("/api/library/ground-textures")
async def list_ground_textures() -> dict:
    """Return per-class status of diffuse/normal/roughness ground textures."""
    from mapng_ai.library_builder.terrain_pack import _CLASS_TO_SLUG, pbr_set
    from PIL import Image
    out = []
    for class_key, slug in _CLASS_TO_SLUG.items():
        s = pbr_set(class_key)
        maps = {}
        for kind in ("diffuse", "normal", "roughness"):
            p = getattr(s, kind, None)
            if p and p.exists():
                w = h = 0
                try:
                    with Image.open(p) as img:
                        w, h = img.size
                except Exception:
                    pass
                # Source: 'custom' if file ends in .png and was uploaded; we
                # mark all PNGs as custom (Poly Haven gives JPG)
                source = "custom" if p.suffix.lower() == ".png" else "polyhaven"
                maps[kind] = {
                    "url": f"/api/pbr/{class_key}/{kind}",
                    "size": p.stat().st_size,
                    "width": w, "height": h,
                    "source": source,
                }
            else:
                maps[kind] = {"url": None, "source": "missing"}
        out.append({"class": class_key, "slug": slug, "maps": maps})
    return {"classes": out}


@app.post("/api/library/ground-textures/{class_key}/{map_kind}")
async def upload_ground_texture(class_key: str, map_kind: str,
                                 file: UploadFile = File(...)) -> dict:
    """Replace a class's ground texture with an uploaded image. PNG only
    (preserves precision; JPG would be re-encoded). Files saved alongside
    the existing Poly Haven JPGs in cache/pbr/<class>/."""
    from mapng_ai.library_builder.terrain_pack import _CLASS_TO_SLUG, PBR_CACHE
    if class_key not in _CLASS_TO_SLUG:
        raise HTTPException(404, f"unknown class: {class_key}")
    if map_kind not in ("diffuse", "normal", "roughness"):
        raise HTTPException(400, "map_kind must be diffuse|normal|roughness")
    body = await file.read()
    if not body:
        raise HTTPException(400, "empty file")

    target_dir_p = PBR_CACHE / class_key
    target_dir_p.mkdir(parents=True, exist_ok=True)
    # Remove any existing variants (jpg + png) so the new one wins
    for ext in (".jpg", ".jpeg", ".png"):
        for stale in target_dir_p.glob(f"{map_kind}{ext}"):
            try: stale.unlink()
            except Exception: pass
    target = target_dir_p / f"{map_kind}.png"
    target.write_bytes(body)
    return {
        "class": class_key, "map_kind": map_kind,
        "saved_bytes": len(body),
        "url": f"/api/pbr/{class_key}/{map_kind}",
    }


@app.delete("/api/library/ground-textures/{class_key}")
async def reset_ground_textures(class_key: str) -> dict:
    """Drop all custom textures for a class so the next pack download
    re-fetches from Poly Haven."""
    from mapng_ai.library_builder.terrain_pack import _CLASS_TO_SLUG, PBR_CACHE
    if class_key not in _CLASS_TO_SLUG:
        raise HTTPException(404, f"unknown class: {class_key}")
    d = PBR_CACHE / class_key
    if d.exists():
        import shutil
        shutil.rmtree(d, ignore_errors=True)
    return {"class": class_key, "reset": True}


@app.get("/api/health")
async def health() -> dict:
    return {"ok": True, "version": app.version}


class PreviewRequest(BaseModel):
    west: float = Field(..., ge=-180, le=180)
    south: float = Field(..., ge=-90, le=90)
    east: float = Field(..., ge=-180, le=180)
    north: float = Field(..., ge=-90, le=90)


@app.post("/api/preview-area")
async def preview_area(req: PreviewRequest) -> dict:
    """Cheap lookup of how many OSM features (buildings, roads, water,
    landuse polygons) live inside a bbox. Lets the UI tell the user
    what they'll get before they hit Generate. Uses Overpass with the
    same caching as the main pipeline so a subsequent Generate hits
    the cache and skips re-fetch."""
    if req.east <= req.west or req.north <= req.south:
        raise HTTPException(400, "Invalid bbox")
    from mapng_ai.sources.overpass import fetch_osm
    bbox = BBox(req.west, req.south, req.east, req.north)
    try:
        osm = await fetch_osm(bbox)
    except Exception as exc:
        raise HTTPException(502, f"Overpass fetch failed: {exc}")
    n_buildings = sum(1 for w in osm.ways if "building" in (w.get("tags") or {}))
    n_roads = sum(1 for w in osm.ways if "highway" in (w.get("tags") or {}))
    n_water = sum(1 for w in osm.ways
                  if (w.get("tags") or {}).get("waterway") or
                     (w.get("tags") or {}).get("natural") == "water")
    n_landuse = sum(1 for w in osm.ways if "landuse" in (w.get("tags") or {}))
    n_barriers = sum(1 for w in osm.ways if "barrier" in (w.get("tags") or {}))
    return {
        "n_buildings": n_buildings,
        "n_roads": n_roads,
        "n_water": n_water,
        "n_landuse": n_landuse,
        "n_barriers": n_barriers,
        "n_ways_total": len(osm.ways),
        "n_relations": len(osm.relations),
    }


@app.post("/api/generate", response_model=GenerateResponse)
async def generate(req: GenerateRequest) -> GenerateResponse:
    if req.east <= req.west or req.north <= req.south:
        raise HTTPException(400, "Invalid bbox: east must be > west, north must be > south")

    job_id = uuid.uuid4().hex[:12]
    out_dir = config.OUTPUT_DIR / job_id
    polygon_ll = None
    if req.polygon and len(req.polygon) >= 3:
        polygon_ll = [(float(p[0]), float(p[1])) for p in req.polygon]
    ctx = JobContext(
        job_id=job_id,
        bbox_ll=BBox(req.west, req.south, req.east, req.north),
        out_dir=out_dir,
        requested_size_m=req.size_m,
        imagery_zoom=req.imagery_zoom,
        polygon_ll=polygon_ll,
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


@app.get("/api/beamng/status")
async def get_beamng_status() -> dict:
    """Reports whether MAPNG_BEAMNG_PATH (or a default Steam location)
    yielded a usable BeamNG install, and how many shapes the scanner
    catalogued. Drives the BeamNG tab in the Asset Browser."""
    from mapng_ai.sources.beamng_assets import install_status
    return install_status()


@app.post("/api/beamng/rescan")
async def post_beamng_rescan() -> dict:
    from mapng_ai.sources.beamng_assets import reset_scan_cache, install_status
    reset_scan_cache()
    return install_status()


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
            "prompt": e.description,        # legacy key for the UI
            "description": e.description,
            "footprint_m": list(e.footprint_m),
            "levels": e.levels,
            "built": glb.exists() and glb.stat().st_size > 0,
            "size_bytes": glb.stat().st_size if glb.exists() else 0,
        })
    return {"entries": out}


# Stub kept for legacy clients — Meshy generation is gone.
@app.post("/api/library/import-pack")
async def post_import_pack(file: UploadFile = File(...)) -> dict:
    """Import a zip of CC0 assets (Quaternius, Kenney, etc.). Files are
    auto-categorised by filename heuristics and copied under
    assets/<category>s/<type>/. Returns a counts summary."""
    from mapng_ai.library_builder.pack_import import import_zip
    body = await file.read()
    if not body:
        raise HTTPException(400, "empty file")
    if not file.filename or not file.filename.lower().endswith(".zip"):
        raise HTTPException(400, "expected a .zip archive")
    result = await asyncio.to_thread(import_zip, body)
    return {
        "imported": result.imported,
        "skipped": result.skipped,
        "by_category": result.by_category,
        "errors": result.errors[:20],   # cap error list size
    }


@app.post("/api/library/build")
async def post_library_build_disabled() -> dict:
    raise HTTPException(
        status_code=410,
        detail="Asset generation has been removed. Import a CC0 pack via "
               "/api/library/import-pack or drag a .glb onto an entry.",
    )


@app.post("/api/library/build/single")
async def build_single_entry_disabled() -> dict:
    raise HTTPException(
        status_code=410,
        detail="Single-entry generation has been removed.",
    )


class PromptOverrideRequest(BaseModel):
    prompt: str | None = None


@app.get("/api/library/entries/{slug}/prompt")
async def get_entry_prompt(slug: str) -> dict:
    """Return current effective prompt + whether it's an override."""
    from mapng_ai.library_builder import CATALOGUE
    from mapng_ai.library_builder.catalogue import get_prompt_override
    entry = next((e for e in CATALOGUE if e.slug == slug), None)
    if entry is None:
        raise HTTPException(404, f"unknown slug: {slug}")
    override = get_prompt_override(slug)
    return {
        "slug": slug,
        "default": entry.description,
        "override": override,
        "effective": override or entry.description,
    }


@app.post("/api/library/entries/{slug}/prompt")
async def set_entry_prompt(slug: str, req: PromptOverrideRequest) -> dict:
    from mapng_ai.library_builder import CATALOGUE
    from mapng_ai.library_builder.catalogue import set_prompt_override
    entry = next((e for e in CATALOGUE if e.slug == slug), None)
    if entry is None:
        raise HTTPException(404, f"unknown slug: {slug}")
    p = (req.prompt or "").strip() or None
    if p == entry.description:
        p = None
    set_prompt_override(slug, p)
    return {"slug": slug, "override": p, "effective": p or entry.description}


@app.post("/api/library/entries/{slug}/upload")
async def upload_entry_glb(slug: str, file: UploadFile = File(...)) -> dict:
    """Replace this entry's GLB with a user-supplied file. Saves to
    assets/buildings|trees|vehicles/<type>/<slug>.glb."""
    from mapng_ai.library_builder import CATALOGUE
    from mapng_ai.library_builder.runner import target_glb, target_dir
    entry = next((e for e in CATALOGUE if e.slug == slug), None)
    if entry is None:
        raise HTTPException(404, f"unknown slug: {slug}")
    if not file.filename or not file.filename.lower().endswith(".glb"):
        raise HTTPException(400, "expected a .glb file")

    target_dir(entry).mkdir(parents=True, exist_ok=True)
    out = target_glb(entry)
    body = await file.read()
    if not body:
        raise HTTPException(400, "empty file")
    out.write_bytes(body)

    # Drop optimised variants — they're stale now
    from mapng_ai.library_builder.optimise import _OPT_CACHE
    import shutil
    for q_dir in _OPT_CACHE.iterdir() if _OPT_CACHE.exists() else []:
        cached = q_dir / out.name
        if cached.exists():
            try: cached.unlink()
            except Exception: pass

    return {"slug": slug, "saved_bytes": len(body), "path": str(out)}


@app.get("/api/library/entries/{slug}/textures")
async def list_entry_textures(slug: str) -> dict:
    """List the textures embedded in this entry's GLB (binary chunks)."""
    from mapng_ai.library_builder import CATALOGUE
    from mapng_ai.library_builder.runner import target_glb
    from pygltflib import GLTF2
    import io
    from PIL import Image

    entry = next((e for e in CATALOGUE if e.slug == slug), None)
    if entry is None:
        raise HTTPException(404, f"unknown slug: {slug}")
    glb = target_glb(entry)
    if not glb.exists():
        return {"slug": slug, "textures": [], "exists": False}

    def _scan() -> list[dict]:
        gltf = GLTF2().load(glb)
        images = gltf.images or []
        blob = gltf.binary_blob() or b""
        results = []
        for i, img in enumerate(images):
            if img.bufferView is None:
                continue
            bv = gltf.bufferViews[img.bufferView]
            data = blob[bv.byteOffset or 0 : (bv.byteOffset or 0) + bv.byteLength]
            mime = (img.mimeType or "image/png")
            w = h = 0
            try:
                with Image.open(io.BytesIO(data)) as pil:
                    w, h = pil.size
            except Exception:
                pass
            results.append({
                "index": i,
                "name": img.name or f"image_{i}",
                "mime": mime,
                "size": len(data),
                "width": w, "height": h,
                "url": f"/api/library/entries/{slug}/textures/{i}",
            })
        return results

    textures = await asyncio.to_thread(_scan)
    return {"slug": slug, "exists": True, "textures": textures}


@app.get("/api/library/entries/{slug}/textures/{idx}")
async def get_entry_texture(slug: str, idx: int) -> Response:
    """Extract one embedded image from the GLB and return it as bytes."""
    from mapng_ai.library_builder import CATALOGUE
    from mapng_ai.library_builder.runner import target_glb
    from pygltflib import GLTF2

    entry = next((e for e in CATALOGUE if e.slug == slug), None)
    if entry is None:
        raise HTTPException(404, f"unknown slug: {slug}")
    glb = target_glb(entry)
    if not glb.exists():
        raise HTTPException(404, f"{slug} not yet generated")

    def _extract() -> tuple[bytes, str]:
        gltf = GLTF2().load(glb)
        images = gltf.images or []
        if idx < 0 or idx >= len(images):
            raise IndexError(f"image index {idx} out of range")
        img = images[idx]
        if img.bufferView is None:
            raise ValueError("image has no bufferView (external uri not supported)")
        bv = gltf.bufferViews[img.bufferView]
        blob = gltf.binary_blob() or b""
        data = blob[bv.byteOffset or 0 : (bv.byteOffset or 0) + bv.byteLength]
        return data, img.mimeType or "image/png"

    try:
        data, mime = await asyncio.to_thread(_extract)
    except (IndexError, ValueError) as exc:
        raise HTTPException(404, str(exc))
    return Response(content=data, media_type=mime,
                    headers={"Cache-Control": "public, max-age=3600"})


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


@app.get("/api/road-decal")
async def get_road_decal() -> FileResponse:
    """The procedural asphalt+centreline tile used for road meshes in the
    preview (and as the BeamNG decal road texture)."""
    from mapng_ai.pipeline.decal_roads import write_road_decal_texture
    p = await asyncio.to_thread(write_road_decal_texture)
    return FileResponse(p, media_type="image/png",
                        headers={"Cache-Control": "public, max-age=86400"})


@app.get("/api/drive-decal")
async def get_drive_decal() -> FileResponse:
    """Brown dirt + wheel-rut tile for driveways."""
    from mapng_ai.pipeline.decal_roads import write_drive_decal_texture
    p = await asyncio.to_thread(write_drive_decal_texture)
    return FileResponse(p, media_type="image/png",
                        headers={"Cache-Control": "public, max-age=86400"})


@app.get("/api/pbr/{class_key}/{map_kind}")
async def get_pbr_map(class_key: str, map_kind: str) -> FileResponse:
    """Serve a Poly Haven PBR map (diffuse / normal / roughness) for the
    terrain shader. 404 if that class hasn't been downloaded."""
    from mapng_ai.library_builder.terrain_pack import pbr_set
    if map_kind not in ("diffuse", "normal", "roughness"):
        raise HTTPException(400, f"map_kind must be diffuse|normal|roughness")
    s = pbr_set(class_key)
    p = getattr(s, map_kind, None)
    if p is None or not p.exists():
        raise HTTPException(404, f"no {map_kind} for {class_key}")
    media = "image/jpeg" if p.suffix.lower() in (".jpg", ".jpeg") else "image/png"
    return FileResponse(p, media_type=media,
                        headers={"Cache-Control": "public, max-age=86400"})


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


# /api/library/meshy-polycount endpoints removed — Meshy integration dropped.


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


# ---- Sky HDRI ---------------------------------------------------------------
@app.get("/api/sky/status")
async def get_sky_status() -> dict:
    from mapng_ai.library_builder.sky_pack import status
    return status()


@app.post("/api/sky/download")
async def post_sky_download(slug: str | None = None) -> dict:
    """Fetch + cache a Poly Haven HDRI. Returns the saved size (bytes).
    If `slug` is omitted, downloads the default overcast NI sky."""
    from mapng_ai.library_builder.sky_pack import download, DEFAULT_SLUG, status
    asset = await download(slug or DEFAULT_SLUG)
    if asset is None:
        raise HTTPException(502, "sky download failed")
    return {"slug": asset.slug, "bytes": asset.bytes, "status": status()}


@app.get("/api/sky/hdr")
async def get_sky_hdr(slug: str | None = None) -> FileResponse:
    """Serve the cached HDRI to the browser (preview.js fetches this)."""
    from mapng_ai.library_builder.sky_pack import cached_path, DEFAULT_SLUG
    p = cached_path(slug or DEFAULT_SLUG)
    if p is None:
        raise HTTPException(404, "no HDRI cached — POST /api/sky/download first")
    return FileResponse(p, media_type="image/vnd.radiance",
                        headers={"Cache-Control": "public, max-age=86400"})


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

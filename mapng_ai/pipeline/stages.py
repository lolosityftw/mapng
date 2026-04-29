"""Pipeline orchestration.

Each stage is an async coroutine taking (`ctx`, `emit`) and mutating ctx.
Phase 0 stubbed everything. Phase 1 wires the first three stages to real
implementations; later stages remain stubs until their phase lands.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from mapng_ai import config
from mapng_ai.pipeline.heightmap import HeightmapResult, build_heightmap
from mapng_ai.pipeline.region import Region, resolve_region
from mapng_ai.sources.base import BBoxLL, ElevationSource, ElevationTile
from mapng_ai.sources.coverage import select_elevation_source


@dataclass
class JobContext:
    job_id: str
    bbox_ll: BBoxLL
    out_dir: Path

    region: Region | None = None
    elevation_source: ElevationSource | None = None
    elevation_tile: ElevationTile | None = None
    heightmap: HeightmapResult | None = None

    artifacts: dict[str, str] = field(default_factory=dict)
    """Public URLs of artefacts the UI can fetch (heightmap preview, etc.)."""


# Compatibility export — older app code still imports BBox from here
BBox = BBoxLL


Emit = Callable[[str, dict], Awaitable[None]]


# ---------------------------------------------------------------------------
# Real stage implementations
# ---------------------------------------------------------------------------
async def stage_region(ctx: JobContext, emit: Emit) -> None:
    ctx.region = resolve_region(ctx.bbox_ll, target_size_m=2000.0,
                                heightmap_size=config.DEFAULT_HEIGHTMAP_SIZE)
    src = await select_elevation_source(ctx.region.fetch_ll)
    ctx.elevation_source = src
    await emit("stage:info", {
        "key": "region",
        "side_m": ctx.region.side_m,
        "heightmap_size": ctx.region.heightmap_size,
        "elevation_source": src.name,
        "elevation_resolution_m": src.native_resolution_m,
    })


async def stage_fetch(ctx: JobContext, emit: Emit) -> None:
    assert ctx.region and ctx.elevation_source
    ctx.elevation_tile = await ctx.elevation_source.fetch(ctx.region.fetch_ll)
    rows, cols = ctx.elevation_tile.elevations_m.shape
    await emit("stage:info", {"key": "fetch", "tile_rows": int(rows), "tile_cols": int(cols)})


async def stage_heightmap(ctx: JobContext, emit: Emit) -> None:
    assert ctx.elevation_tile and ctx.region
    # rasterio + numpy work is CPU-bound; offload to a thread to keep the event loop free
    ctx.heightmap = await asyncio.to_thread(
        build_heightmap, ctx.elevation_tile, ctx.region, ctx.out_dir
    )
    rel = lambda p: f"/api/jobs/{ctx.job_id}/files/{p.name}"
    ctx.artifacts["heightmap_preview"] = rel(ctx.heightmap.preview_png_path)
    ctx.artifacts["heightmap16"] = rel(ctx.heightmap.png16_path)
    ctx.artifacts["geotiff"] = rel(ctx.heightmap.geotiff_path)
    await emit("stage:info", {
        "key": "heightmap",
        "min_m": ctx.heightmap.min_m,
        "max_m": ctx.heightmap.max_m,
        "preview_url": ctx.artifacts["heightmap_preview"],
        "side_m": ctx.region.side_m,
    })


# ---------------------------------------------------------------------------
# Stub stages (replaced phase-by-phase)
# ---------------------------------------------------------------------------
async def _stub(stage_key: str, seconds: float):
    async def runner(ctx: JobContext, emit: Emit) -> None:
        steps = 4
        for i in range(steps):
            await asyncio.sleep(seconds / steps)
            await emit("stage:progress", {"key": stage_key, "fraction": (i + 1) / steps})
    return runner


# ---------------------------------------------------------------------------
# Stage registry
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Stage:
    key: str
    label: str
    runner: Callable[[JobContext, Emit], Awaitable[None]]


STAGES: tuple[Stage, ...] = (
    Stage("region",    "Resolve region",                stage_region),
    Stage("fetch",     "Fetch DEM / imagery / OSM",     stage_fetch),
    Stage("heightmap", "Build heightmap",               stage_heightmap),
    Stage("segment",   "Land-cover segmentation",       None),  # filled at runtime
    Stage("splat",     "Material splatting",            None),
    Stage("place",     "Object placement",              None),
    Stage("export",    "BeamNG export",                 None),
)


async def run_pipeline(ctx: JobContext, emit: Emit) -> None:
    await emit("pipeline:start", {
        "bbox": ctx.bbox_ll.__dict__,
        "stages": [s.key for s in STAGES],
        "job_id": ctx.job_id,
    })
    for stage in STAGES:
        await emit("stage:start", {"key": stage.key, "label": stage.label})
        runner = stage.runner or await _stub(stage.key, 0.6)
        try:
            await runner(ctx, emit)
        except Exception as exc:
            await emit("stage:error", {"key": stage.key, "message": str(exc)})
            raise
        await emit("stage:done", {"key": stage.key, "artifacts": ctx.artifacts})
    await emit("pipeline:done", {"artifacts": ctx.artifacts})

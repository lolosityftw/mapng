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
from mapng_ai.assets.placeholder import PlaceholderProvider
from mapng_ai.pipeline.beamng_level import LevelPackage, write_level_package
from mapng_ai.pipeline.classmap import build_class_map
from mapng_ai.pipeline.heightmap import HeightmapResult, build_heightmap
from mapng_ai.pipeline.placement import BuildingPlacement, place_buildings
from mapng_ai.pipeline.region import Region, resolve_region
from mapng_ai.pipeline.splatting import SplatResult, build_splat
from mapng_ai.sources.base import BBoxLL, ElevationSource, ElevationTile
from mapng_ai.sources.coverage import select_elevation_source
from mapng_ai.sources.overpass import OSMData, fetch_osm

import numpy as np


@dataclass
class JobContext:
    job_id: str
    bbox_ll: BBoxLL
    out_dir: Path

    region: Region | None = None
    elevation_source: ElevationSource | None = None
    elevation_tile: ElevationTile | None = None
    heightmap: HeightmapResult | None = None
    osm: OSMData | None = None
    class_map: "np.ndarray | None" = None
    splat: SplatResult | None = None
    buildings: list[BuildingPlacement] = field(default_factory=list)
    level_package: LevelPackage | None = None

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
    # Fetch elevation + OSM in parallel
    elev_task = asyncio.create_task(ctx.elevation_source.fetch(ctx.region.fetch_ll))
    osm_task = asyncio.create_task(fetch_osm(ctx.region.fetch_ll))
    ctx.elevation_tile = await elev_task
    ctx.osm = await osm_task
    rows, cols = ctx.elevation_tile.elevations_m.shape
    n_buildings = sum(1 for w in ctx.osm.ways if "building" in (w.get("tags") or {}))
    n_roads = sum(1 for w in ctx.osm.ways if "highway" in (w.get("tags") or {}))
    await emit("stage:info", {
        "key": "fetch",
        "tile_rows": int(rows), "tile_cols": int(cols),
        "osm_buildings": n_buildings, "osm_roads": n_roads,
        "osm_ways_total": len(ctx.osm.ways),
    })


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


async def stage_segment(ctx: JobContext, emit: Emit) -> None:
    """Phase 4 MVP: rasterise OSM features into a class map (no AI yet).

    The pretrained-model upgrade per spec §4.1 will replace this function and
    keep the same `class_map` output shape/semantics."""
    assert ctx.osm and ctx.region
    size = ctx.region.heightmap_size
    ctx.class_map = await asyncio.to_thread(build_class_map, ctx.osm, ctx.region, size)
    # Class histogram for the UI
    unique, counts = np.unique(ctx.class_map, return_counts=True)
    total = float(counts.sum())
    hist = [{"id": int(u), "pct": round(float(c) / total * 100, 1)} for u, c in zip(unique, counts)]
    await emit("stage:info", {"key": "segment", "source": "osm-rasterise", "histogram": hist})


async def stage_splat(ctx: JobContext, emit: Emit) -> None:
    assert ctx.class_map is not None
    ctx.splat = await asyncio.to_thread(build_splat, ctx.class_map, ctx.out_dir)
    layers_payload = [
        {"key": l.cls.key, "label": l.cls.label, "color": list(l.cls.color_rgb),
         "coverage_pct": round(l.coverage_pct, 1)}
        for l in ctx.splat.layers
    ]
    ctx.artifacts["terrain_combined"] = (
        f"/api/jobs/{ctx.job_id}/files/{ctx.splat.combined_diffuse_path.name}"
    )
    await emit("stage:info", {
        "key": "splat",
        "layers": layers_payload,
        "n_layers": len(ctx.splat.layers),
        "combined_url": ctx.artifacts["terrain_combined"],
    })


async def stage_place(ctx: JobContext, emit: Emit) -> None:
    assert ctx.osm and ctx.region and ctx.heightmap
    provider = PlaceholderProvider()
    ctx.buildings = await asyncio.to_thread(
        place_buildings, ctx.osm, ctx.region, ctx.heightmap.elevations_m, provider
    )
    # Send placement summary so the preview can draw boxes
    payload = [
        {
            "x": p.x_m, "y": p.y_m, "z": p.z_m,
            "yaw": p.yaw_rad,
            "scale": list(p.scale_xyz),
            "color": p.asset.color_hex,
            "type": p.asset.type_label,
        }
        for p in ctx.buildings
    ]
    await emit("stage:info", {"key": "place", "buildings": payload})


async def stage_export(ctx: JobContext, emit: Emit) -> None:
    assert ctx.heightmap and ctx.region
    level_name = f"mapng_{ctx.job_id}"
    pkg = await asyncio.to_thread(
        write_level_package,
        level_name=level_name,
        heightmap_m=ctx.heightmap.elevations_m,
        side_m=ctx.region.side_m,
        out_dir=ctx.out_dir,
        buildings=ctx.buildings,
        splat=ctx.splat,
    )
    ctx.level_package = pkg
    ctx.artifacts["level_zip"] = f"/api/jobs/{ctx.job_id}/files/{pkg.zip_path.name}"
    await emit("stage:info", {
        "key": "export",
        "level_name": level_name,
        "zip_url": ctx.artifacts["level_zip"],
        "zip_bytes": pkg.zip_path.stat().st_size,
        "spawn_xyz": list(pkg.spawn_xyz),
        "n_buildings": len(ctx.buildings),
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
    Stage("segment",   "Land-cover segmentation",       stage_segment),
    Stage("splat",     "Material splatting",            stage_splat),
    Stage("place",     "Object placement",              stage_place),
    Stage("export",    "BeamNG export",                 stage_export),
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

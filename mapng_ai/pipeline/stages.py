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
from mapng_ai.assets.chain import ProviderChain
from mapng_ai.pipeline.beamng_level import LevelPackage, write_level_package
from mapng_ai.pipeline.classmap import build_class_map
from mapng_ai.pipeline.decal_roads import DecalRoad, extract_decal_roads
from mapng_ai.pipeline.foliage import FoliageResult, place_foliage
from mapng_ai.pipeline.heightmap import HeightmapResult, build_heightmap
from mapng_ai.pipeline.hsv_seg import classify_imagery, fuse_segmentation
from mapng_ai.pipeline.imagery import ImageryResult, reproject_imagery
from mapng_ai.pipeline.placement import BuildingPlacement, place_buildings
from mapng_ai.pipeline.region import Region, resolve_region
from mapng_ai.pipeline.splatting import SplatResult, build_splat
from mapng_ai.sources.base import BBoxLL, ElevationSource, ElevationTile
from mapng_ai.sources.coverage import select_elevation_source
from mapng_ai.sources.esri import ImageryTile, default_imagery_source
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
    imagery_tile: ImageryTile | None = None
    imagery: ImageryResult | None = None
    class_map: "np.ndarray | None" = None
    splat: SplatResult | None = None
    buildings: list[BuildingPlacement] = field(default_factory=list)
    foliage: FoliageResult | None = None
    decal_roads: list[DecalRoad] = field(default_factory=list)
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
    imagery_source = default_imagery_source()
    # All three fetches in parallel
    elev_task = asyncio.create_task(ctx.elevation_source.fetch(ctx.region.fetch_ll))
    osm_task = asyncio.create_task(fetch_osm(ctx.region.fetch_ll))
    img_task = asyncio.create_task(imagery_source.fetch(ctx.region.fetch_ll))
    ctx.elevation_tile = await elev_task
    ctx.osm = await osm_task
    try:
        ctx.imagery_tile = await img_task
    except Exception as exc:
        # Imagery is best-effort — pipeline continues with procedural ground texture
        await emit("stage:info", {"key": "fetch", "imagery_error": str(exc)})
        ctx.imagery_tile = None

    rows, cols = ctx.elevation_tile.elevations_m.shape
    n_buildings = sum(1 for w in ctx.osm.ways if "building" in (w.get("tags") or {}))
    n_roads = sum(1 for w in ctx.osm.ways if "highway" in (w.get("tags") or {}))
    payload = {
        "key": "fetch",
        "tile_rows": int(rows), "tile_cols": int(cols),
        "osm_buildings": n_buildings, "osm_roads": n_roads,
        "osm_ways_total": len(ctx.osm.ways),
    }
    if ctx.imagery_tile is not None:
        ih, iw = ctx.imagery_tile.rgb.shape[:2]
        payload.update({"imagery_zoom": ctx.imagery_tile.zoom,
                        "imagery_rows": ih, "imagery_cols": iw,
                        "imagery_source": imagery_source.name})
    await emit("stage:info", payload)


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
    """Combine OSM rasterise (deterministic) with HSV imagery classification
    (per-pixel detail). OSM wins for trusted classes (roads, water); imagery
    refines the rest."""
    assert ctx.osm and ctx.region
    size = ctx.region.heightmap_size

    # 1) OSM rasterise (always)
    osm_map = await asyncio.to_thread(build_class_map, ctx.osm, ctx.region, size)

    used_imagery = False
    if ctx.imagery_tile is not None:
        # 2) Reproject imagery to ITM at heightmap resolution
        ctx.imagery = await asyncio.to_thread(
            reproject_imagery, ctx.imagery_tile, ctx.region, ctx.out_dir, size
        )
        ctx.artifacts["satellite"] = (
            f"/api/jobs/{ctx.job_id}/files/{ctx.imagery.sat_png_path.name}"
        )
        ctx.artifacts["satellite_normal"] = (
            f"/api/jobs/{ctx.job_id}/files/{ctx.imagery.normal_png_path.name}"
        )
        # 3) HSV classify the ITM-aligned imagery
        img_map = await asyncio.to_thread(classify_imagery, ctx.imagery.rgb)
        # 4) Fuse — OSM trusted classes win over imagery
        ctx.class_map = await asyncio.to_thread(fuse_segmentation, osm_map, img_map)
        used_imagery = True
    else:
        ctx.class_map = osm_map

    unique, counts = np.unique(ctx.class_map, return_counts=True)
    total = float(counts.sum())
    hist = [{"id": int(u), "pct": round(float(c) / total * 100, 1)} for u, c in zip(unique, counts)]
    payload = {
        "key": "segment",
        "source": "osm+imagery-hsv" if used_imagery else "osm-rasterise",
        "histogram": hist,
    }
    if used_imagery:
        payload["satellite_url"] = ctx.artifacts["satellite"]
        payload["satellite_normal_url"] = ctx.artifacts["satellite_normal"]
    await emit("stage:info", payload)


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
    provider = ProviderChain()
    hm = ctx.heightmap.elevations_m

    ctx.buildings = await asyncio.to_thread(
        place_buildings, ctx.osm, ctx.region, hm, provider
    )
    ctx.foliage = await asyncio.to_thread(place_foliage, ctx.osm, ctx.region, hm)
    ctx.decal_roads = await asyncio.to_thread(extract_decal_roads, ctx.osm, ctx.region, hm)

    # Three.js preview payload
    buildings_payload = [
        {
            "x": p.x_m, "y": p.y_m, "z": p.z_m,
            "yaw": p.yaw_rad,
            "scale": list(p.scale_xyz),
            "color": p.asset.color_hex,
            "type": p.asset.type_label,
            "shape": p.asset.shape_relpath,
        }
        for p in ctx.buildings
    ]
    trees_payload = [
        {"x": t.x, "y": t.y, "z": t.z, "yaw": t.yaw,
         "scale": list(t.scale_xyz), "shape": t.shape_relpath}
        for t in ctx.foliage.trees
    ]
    hedges_payload = [
        {"x": h.x, "y": h.y, "z": h.z, "yaw": h.yaw,
         "length": h.length_m, "width": h.width_m, "height": h.height_m}
        for h in ctx.foliage.hedges
    ]
    roads_payload = [
        {"width": r.width_m, "nodes": [list(n) for n in r.nodes_xyz]}
        for r in ctx.decal_roads
    ]
    # Diagnostics: how many of each placement came from where
    src_counts = {"library": 0, "meshy": 0, "placeholder": 0}
    for b in ctx.buildings:
        rel = b.asset.shape_relpath
        if rel.startswith("art/shapes/buildings_lib/"):
            src_counts["library"] += 1
        elif rel.startswith("art/shapes/buildings_ai/"):
            src_counts["meshy"] += 1
        else:
            src_counts["placeholder"] += 1

    tree_lib_count = sum(
        1 for t in ctx.foliage.trees
        if t.shape_relpath.startswith("art/shapes/trees_lib/")
    )

    await emit("stage:info", {
        "key": "place",
        "buildings": buildings_payload,
        "trees": trees_payload,
        "hedges": hedges_payload,
        "roads": roads_payload,
        "n_buildings": len(ctx.buildings),
        "buildings_by_source": src_counts,
        "n_trees": len(ctx.foliage.trees),
        "trees_from_library": tree_lib_count,
        "n_hedges": len(ctx.foliage.hedges),
        "n_roads": len(ctx.decal_roads),
    })


async def stage_export(ctx: JobContext, emit: Emit) -> None:
    assert ctx.heightmap and ctx.region
    level_name = f"mapng_{ctx.job_id}"
    terrain_png = None
    if ctx.imagery is not None:
        terrain_png = ctx.imagery.sat_png_path.read_bytes()

    pkg = await asyncio.to_thread(
        write_level_package,
        level_name=level_name,
        heightmap_m=ctx.heightmap.elevations_m,
        side_m=ctx.region.side_m,
        out_dir=ctx.out_dir,
        buildings=ctx.buildings,
        foliage=ctx.foliage,
        decal_roads=ctx.decal_roads,
        splat=ctx.splat,
        terrain_png_bytes=terrain_png,
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
        "n_trees": len(ctx.foliage.trees) if ctx.foliage else 0,
        "n_hedges": len(ctx.foliage.hedges) if ctx.foliage else 0,
        "n_roads": len(ctx.decal_roads),
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

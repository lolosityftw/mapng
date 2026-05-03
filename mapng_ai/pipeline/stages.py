"""Pipeline orchestration.

Each stage is an async coroutine taking (`ctx`, `emit`) and mutating ctx.
Phase 0 stubbed everything. Phase 1 wires the first three stages to real
implementations; later stages remain stubs until their phase lands.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from pyproj import Transformer
from shapely.geometry import LineString, Point, Polygon
from shapely.prepared import prep

# Cached: pyproj transformer setup is non-trivial; the polygon-clip
# function builds many of these per request without caching.
_LL_TO_ITM = Transformer.from_crs("EPSG:4326", "EPSG:2157", always_xy=True)

log = logging.getLogger(__name__)

from mapng_ai import config
from mapng_ai.assets.chain import ProviderChain
from mapng_ai.pipeline.beamng_level import LevelPackage, write_level_package
from mapng_ai.pipeline.classmap import build_class_map
from mapng_ai.pipeline.decal_roads import DecalRoad, extract_decal_roads, extract_driveways
from mapng_ai.assets.beamng_provider import BeamNGAssetProvider
from mapng_ai.pipeline.foliage import FoliageResult, TreePlacement, place_foliage, add_garden_features
from mapng_ai.pipeline.heightmap import HeightmapResult, build_heightmap
from mapng_ai.pipeline.hsv_seg import classify_imagery, fuse_segmentation
from mapng_ai.pipeline.imagery import ImageryResult, reproject_imagery
from mapng_ai.pipeline.placement import BuildingPlacement, place_buildings
from mapng_ai.pipeline.region import Region, resolve_region
from mapng_ai.pipeline.splatting import SplatResult, build_detailed_terrain, build_splat
from mapng_ai.sources.base import BBoxLL, ElevationSource, ElevationTile
from mapng_ai.sources.beamng_assets import install_status as _bn_install_status
from mapng_ai.sources.coverage import select_elevation_source
from mapng_ai.sources.esri import ImageryTile, default_imagery_source
from mapng_ai.sources.overpass import OSMData, fetch_osm

import numpy as np


@dataclass
class JobContext:
    job_id: str
    bbox_ll: BBoxLL
    out_dir: Path

    # Optional: explicit terrain side length in metres. If None, we use
    # the larger of the bbox's projected width/height (clamped 500..8000 m).
    requested_size_m: float | None = None
    # Optional: imagery zoom override (default 17 → ~0.69 m/px in NI).
    imagery_zoom: int | None = None
    # Optional: user-drawn polygon as [(lon, lat), ...]. When set, all
    # placements (buildings / trees / hedges / driveways) are clipped to
    # the polygon so the level only contains content inside the user's
    # selected shape.
    polygon_ll: list[tuple[float, float]] | None = None

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


def _clip_to_polygon(ctx: JobContext) -> int:
    """Drop every placement whose terrain-local XY falls outside the
    user's drawn polygon. Returns the number of placements removed.

    Roads/driveways are pruned by trimming each line to the polygon.
    Buildings, trees, and hedges are dropped entirely if their centre
    is outside.
    """
    if not ctx.polygon_ll or len(ctx.polygon_ll) < 3 or ctx.region is None:
        return 0

    # Project polygon vertices to terrain-local ITM
    cx_w = (ctx.region.working_itm.west + ctx.region.working_itm.east) / 2
    cy_w = (ctx.region.working_itm.south + ctx.region.working_itm.north) / 2
    pts: list[tuple[float, float]] = []
    for lon, lat in ctx.polygon_ll:
        x, y = _LL_TO_ITM.transform(lon, lat)
        pts.append((x - cx_w, y - cy_w))
    try:
        poly = Polygon(pts)
        if not poly.is_valid:
            poly = poly.buffer(0)
        if poly.is_empty:
            return 0
    except Exception:
        return 0

    # Prepared geometry: shapely indexes the polygon for fast contains();
    # ~10× faster than poly.contains for repeated point queries which is
    # exactly what this function does.
    prepared = prep(poly)
    contains = prepared.contains
    n_dropped = 0

    # Buildings — drop those whose centroid is outside the polygon.
    if ctx.buildings:
        kept = []
        for b in ctx.buildings:
            if contains(Point(b.x_m, b.y_m)):
                kept.append(b)
            else:
                n_dropped += 1
        ctx.buildings = kept

    # Foliage trees + hedges — same point-in-polygon test.
    if ctx.foliage:
        kept_trees = []
        for t in ctx.foliage.trees:
            if contains(Point(t.x, t.y)):
                kept_trees.append(t)
            else:
                n_dropped += 1
        kept_hedges = []
        for h in ctx.foliage.hedges:
            if contains(Point(h.x, h.y)):
                kept_hedges.append(h)
            else:
                n_dropped += 1
        ctx.foliage = FoliageResult(
            trees=kept_trees, hedges=kept_hedges,
            forest_polys=ctx.foliage.forest_polys,
            standalone_trees=ctx.foliage.standalone_trees,
        )

    # Decal roads + driveways — clip each line to the polygon. A line
    # may split into multiple sub-lines; we keep each sub-line as its
    # own DecalRoad entry.
    if ctx.decal_roads:
        clipped: list[DecalRoad] = []
        for r in ctx.decal_roads:
            try:
                line = LineString([(n[0], n[1]) for n in r.nodes_xyz])
                if not line.is_valid or line.length < 1e-3:
                    continue
                inside = line.intersection(poly)
            except Exception:
                clipped.append(r)
                continue
            if inside.is_empty:
                n_dropped += 1
                continue
            geoms = [inside] if hasattr(inside, "coords") else list(getattr(inside, "geoms", []))
            for g in geoms:
                if not isinstance(g, LineString) or g.length < 1.0:
                    continue
                # Re-attach z (interpolate from original polyline) — the
                # decal road needs a z per node.
                new_nodes: list[tuple[float, float, float]] = []
                for x, y in g.coords:
                    z = _interp_z_along(r.nodes_xyz, x, y)
                    new_nodes.append((x, y, z))
                if len(new_nodes) >= 2:
                    clipped.append(DecalRoad(
                        osm_id=r.osm_id, name=r.name,
                        width_m=r.width_m, nodes_xyz=new_nodes,
                        material=r.material,
                    ))
        ctx.decal_roads = clipped

    return n_dropped


def _interp_z_along(nodes_xyz, x: float, y: float) -> float:
    """Pick z from the closest of the line's original nodes. Quick + good
    enough — the polygon clip slices straight through linear pieces."""
    best_d2 = float("inf")
    best_z = 0.0
    for nx, ny, nz in nodes_xyz:
        d2 = (nx - x) ** 2 + (ny - y) ** 2
        if d2 < best_d2:
            best_d2 = d2
            best_z = nz
    return best_z


# ---------------------------------------------------------------------------
# Real stage implementations
# ---------------------------------------------------------------------------
async def stage_region(ctx: JobContext, emit: Emit) -> None:
    # `target_size_m=None` means "use the bbox the user actually drew"
    # (clamped to 500..8000 m). The job request can override by setting
    # ctx.requested_size_m to a specific length.
    requested = getattr(ctx, "requested_size_m", None)
    ctx.region = resolve_region(ctx.bbox_ll, target_size_m=requested,
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
    # Honour the per-job imagery zoom override if set
    if ctx.imagery_zoom is not None:
        from mapng_ai.sources.esri import EsriSource
        imagery_source = EsriSource(zoom=ctx.imagery_zoom)
    else:
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
    layers_payload = []
    for l in ctx.splat.layers:
        opacity_url = f"/api/jobs/{ctx.job_id}/files/opacity_{l.cls.key}.png"
        diffuse_url = (f"/api/pbr/{l.cls.key}/diffuse" if l.source == "polyhaven" else None)
        normal_url = (f"/api/pbr/{l.cls.key}/normal" if l.source == "polyhaven"
                      and l.normal_path is not None else None)
        layers_payload.append({
            "key": l.cls.key,
            "label": l.cls.label,
            "color": list(l.cls.color_rgb),
            "coverage_pct": round(l.coverage_pct, 1),
            "source": l.source,
            "opacity_url": opacity_url,
            "diffuse_url": diffuse_url,
            "normal_url": normal_url,
        })
    ctx.artifacts["terrain_combined"] = (
        f"/api/jobs/{ctx.job_id}/files/{ctx.splat.combined_diffuse_path.name}"
    )

    # If real Esri imagery and PBR detail tiles are both available, bake a
    # composite terrain texture for the preview pane.
    detailed_url: str | None = None
    has_polyhaven = any(l.source == "polyhaven" for l in ctx.splat.layers)
    if ctx.imagery is not None and has_polyhaven:
        detailed_path = ctx.out_dir / "terrain_detailed.png"
        await asyncio.to_thread(
            build_detailed_terrain,
            layers=ctx.splat.layers,
            sat_rgb=ctx.imagery.rgb,
            out_path=detailed_path,
        )
        ctx.splat.detailed_diffuse_path = detailed_path
        ctx.artifacts["terrain_detailed"] = (
            f"/api/jobs/{ctx.job_id}/files/{detailed_path.name}"
        )
        detailed_url = ctx.artifacts["terrain_detailed"]

    await emit("stage:info", {
        "key": "splat",
        "layers": layers_payload,
        "n_layers": len(ctx.splat.layers),
        "combined_url": ctx.artifacts["terrain_combined"],
        "detailed_url": detailed_url,
        "has_pbr_detail": has_polyhaven,
    })


async def stage_place(ctx: JobContext, emit: Emit) -> None:
    assert ctx.osm and ctx.region and ctx.heightmap
    provider = ProviderChain()
    hm = ctx.heightmap.elevations_m

    ctx.buildings = await asyncio.to_thread(
        place_buildings, ctx.osm, ctx.region, hm, provider
    )
    # place_foliage now takes the class map so it can fall back to a
    # synthesised field-boundary grid when OSM has few farmland polys.
    ctx.foliage = await asyncio.to_thread(
        place_foliage, ctx.osm, ctx.region, hm, class_map=ctx.class_map,
    )
    # Garden walls + sheds for residential buildings — extend foliage.hedges in-place.
    await asyncio.to_thread(
        add_garden_features,
        hedges=ctx.foliage.hedges, buildings=ctx.buildings,
        region=ctx.region, heightmap_m=hm,
    )
    ctx.decal_roads = await asyncio.to_thread(extract_decal_roads, ctx.osm, ctx.region, hm)
    # Driveways from isolated buildings to nearest road. Tacked onto
    # decal_roads so the BeamNG zip writer treats them like any other
    # decal (just with the dirt material).
    drives = await asyncio.to_thread(
        extract_driveways, ctx.osm, ctx.region, hm, ctx.buildings,
    )
    ctx.decal_roads.extend(drives)

    # Polygon clipping — when the user drew a non-rectangular shape,
    # prune every placement whose XY falls outside the polygon. This
    # lets users select arbitrary areas (rivers, settlement outlines,
    # driving routes) and only get content inside those bounds.
    n_clipped = await asyncio.to_thread(_clip_to_polygon, ctx)
    if n_clipped:
        await emit("stage:info", {"key": "place", "n_clipped": n_clipped})

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
         "length": h.length_m, "width": h.width_m, "height": h.height_m,
         "material": h.material}
        for h in ctx.foliage.hedges
    ]
    roads_payload = [
        {"width": r.width_m, "nodes": [list(n) for n in r.nodes_xyz],
         "material": r.material}
        for r in ctx.decal_roads
    ]
    # Diagnostics: how many of each placement came from where. The export
    # stage may later substitute placeholders for BeamNG cross-level
    # references — that count is reported separately as `beamng_subs`.
    src_counts = {"library": 0, "placeholder": 0}
    for b in ctx.buildings:
        rel = b.asset.shape_relpath
        if rel.startswith("art/shapes/buildings_lib/"):
            src_counts["library"] += 1
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

    # Prefer the detailed terrain composite (satellite + per-class PBR tiles)
    # over the raw satellite for BeamNG's wide-area diffuse — it gives the
    # in-game terrain a head-start on detail before BeamNG's own per-material
    # blending kicks in. Falls back to the satellite if no PBR tiles, then
    # to None (procedural blend).
    terrain_png = None
    if ctx.splat and ctx.splat.detailed_diffuse_path is not None:
        terrain_png = ctx.splat.detailed_diffuse_path.read_bytes()
    elif ctx.imagery is not None:
        terrain_png = ctx.imagery.sat_png_path.read_bytes()

    # BeamNG asset reference mode — ON by default, restricted to ITALY ONLY.
    # Italy is rural Mediterranean countryside (village houses, stone walls,
    # churches, dirt tracks) — closest vanilla match for NI villages. The
    # earlier kanji-textured buildings issue was caused by scanning multiple
    # vanilla levels simultaneously; locking to Italy avoids that. Set
    # MAPNG_BEAMNG_REFS=0 to fall back to placeholder COLLADA buildings.
    import os as _os
    refs_enabled = _os.environ.get("MAPNG_BEAMNG_REFS", "1") == "1"
    export_buildings = ctx.buildings
    export_foliage = ctx.foliage
    beamng_subs = 0
    beamng_provider_status = "disabled (set MAPNG_BEAMNG_REFS=1 to enable)"
    try:
        if not refs_enabled:
            raise RuntimeError("BeamNG ref-mode disabled")
        bn_st = _bn_install_status()
        beamng_provider_status = (
            f"detected: {bn_st['path']} — {bn_st['asset_count']} shapes"
            if bn_st.get("detected") else "NOT DETECTED — set MAPNG_BEAMNG_PATH"
        )
        bp = BeamNGAssetProvider()
        if bp.can_provide("building") and ctx.buildings:
            new_blds = []
            for b in ctx.buildings:
                fp = b.scale_xyz[0] * b.scale_xyz[1]
                lev = max(1, int(round(b.scale_xyz[2] / 3.0)))
                sub = bp.get_building(fp, lev, b.asset.type_label, b.osm_id)
                if sub is not None:
                    new_blds.append(BuildingPlacement(
                        osm_id=b.osm_id, asset=sub,
                        x_m=b.x_m, y_m=b.y_m, z_m=b.z_m,
                        yaw_rad=b.yaw_rad, scale_xyz=b.scale_xyz,
                    ))
                    beamng_subs += 1
                else:
                    new_blds.append(b)
            export_buildings = new_blds
        if bp.can_provide("tree") and ctx.foliage and ctx.foliage.trees:
            new_trees = []
            for t in ctx.foliage.trees:
                pick = bp.pick_tree(int(abs(hash((round(t.x, 1), round(t.y, 1)))) & 0xFFFFFFFF))
                if pick is not None:
                    new_trees.append(TreePlacement(
                        x=t.x, y=t.y, z=t.z, scale_xyz=t.scale_xyz, yaw=t.yaw,
                        shape_relpath=pick.relpath.lstrip("/"),
                        species=pick.type,
                    ))
                else:
                    new_trees.append(t)
            export_foliage = FoliageResult(
                trees=new_trees, hedges=ctx.foliage.hedges,
                forest_polys=ctx.foliage.forest_polys,
                standalone_trees=ctx.foliage.standalone_trees,
            )
    except Exception as exc:
        # The disabled-by-default RuntimeError is expected; log it as
        # info, not as an exception. Anything else is a real error and
        # gets the full traceback in server logs.
        msg = str(exc)
        if "ref-mode disabled" in msg:
            pass        # silent skip — user opted out
        else:
            log.exception("BeamNG asset substitution failed")
            await emit("stage:info", {
                "key": "export",
                "beamng_subs": 0,
                "beamng_error": f"{type(exc).__name__}: {exc}",
            })

    pkg = await asyncio.to_thread(
        write_level_package,
        level_name=level_name,
        heightmap_m=ctx.heightmap.elevations_m,
        side_m=ctx.region.side_m,
        out_dir=ctx.out_dir,
        buildings=export_buildings,
        foliage=export_foliage,
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
        "n_buildings": len(export_buildings),
        "n_trees": len(export_foliage.trees) if export_foliage else 0,
        "n_hedges": len(export_foliage.hedges) if export_foliage else 0,
        "n_roads": len(ctx.decal_roads),
        "beamng_subs": beamng_subs,
        "beamng_provider": beamng_provider_status,
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

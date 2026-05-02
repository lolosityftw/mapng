"""Stage 5 polish — DecalRoad emission for BeamNG.

The splat layers already paint asphalt onto the terrain — DecalRoads add
*proper road shape* on top with width tapering and (eventually) painted
lane markings.

For the MVP we ship a single procedural road decal texture (asphalt with a
dashed yellow centreline) and a single TerrainMaterial reference; every
extracted OSM highway gets one DecalRoad placed along its centreline,
sampled at terrain height.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image
from pyproj import Transformer

from mapng_ai import config
from mapng_ai.pipeline.region import Region, sample_terrain_height as _z_at
from mapng_ai.sources.overpass import OSMData, way_line_ll


_LL_TO_ITM = Transformer.from_crs("EPSG:4326", "EPSG:2157", always_xy=True)


# Highway → (decal width metres, smoothness)
_DECAL_WIDTHS: dict[str, float] = {
    "motorway":     14.0,
    "trunk":        12.0,
    "primary":      10.0,
    "secondary":    9.0,
    "tertiary":     8.0,
    "unclassified": 7.0,
    "residential":  6.0,
    "living_street": 5.0,
    "service":      5.0,
    "track":        4.0,
}


@dataclass(frozen=True)
class DecalRoad:
    osm_id: int
    name: str
    width_m: float
    nodes_xyz: list[tuple[float, float, float]]
    # 'asphalt' = full road decal texture, 'dirt' = driveway / track variant.
    # The BeamNG zip writer maps each value to a different Material so the
    # in-game decals look distinct.
    material: str = "asphalt"


# `_z_at` is the canonical height sampler from region.py (imported above).


def extract_decal_roads(osm: OSMData, region: Region, heightmap_m: np.ndarray) -> list[DecalRoad]:
    cx_world = (region.working_itm.west + region.working_itm.east) / 2
    cy_world = (region.working_itm.south + region.working_itm.north) / 2
    half = region.side_m / 2

    out: list[DecalRoad] = []
    for w in osm.ways:
        tags = w.get("tags") or {}
        hwy = tags.get("highway")
        if hwy not in _DECAL_WIDTHS:
            continue
        line_ll = way_line_ll(w, osm.nodes)
        if len(line_ll) < 2:
            continue
        # Reproject and shift to terrain-local
        lon, lat = zip(*line_ll)
        xs, ys = _LL_TO_ITM.transform(list(lon), list(lat))
        xs_local = [x - cx_world for x in xs]
        ys_local = [y - cy_world for y in ys]
        nodes: list[tuple[float, float, float]] = []
        for x, y in zip(xs_local, ys_local):
            if abs(x) > half or abs(y) > half:
                continue
            z = _z_at(heightmap_m, region.side_m, x, y) + 0.05  # tiny lift to dodge z-fighting
            nodes.append((x, y, z))
        if len(nodes) < 2:
            continue
        # Roundabouts are usually closed loops with junction=roundabout.
        # We bump their width slightly so they read as a separate ring on
        # the decal layer + close the polyline if needed.
        is_round = tags.get("junction") == "roundabout"
        width = _DECAL_WIDTHS[hwy]
        if is_round:
            width = max(width, 6.0)
            if len(nodes) > 2 and nodes[0] != nodes[-1]:
                nodes.append(nodes[0])     # close the loop
        out.append(DecalRoad(
            osm_id=int(w["id"]),
            name=tags.get("name", "roundabout" if is_round else "road"),
            width_m=width,
            nodes_xyz=nodes,
        ))
    return out


# ---------------------------------------------------------------------------
# Driveway tracing — buildings > 30 m from any road get a thin dirt strip
# from their centroid to the nearest road centreline.
# ---------------------------------------------------------------------------
def extract_driveways(osm: OSMData, region: Region, heightmap_m: np.ndarray,
                      buildings) -> list[DecalRoad]:
    """For each building far enough from the road network, emit a single-
    segment dirt DecalRoad linking it to the nearest road. Skips buildings
    that are already adjacent (< 18 m) and those farther than 350 m
    (probably misplaced or off-grid).
    """
    from shapely.geometry import LineString, Point
    from shapely.ops import nearest_points, unary_union

    cx_world = (region.working_itm.west + region.working_itm.east) / 2
    cy_world = (region.working_itm.south + region.working_itm.north) / 2
    half = region.side_m / 2

    # Build the road network as a single MultiLineString in terrain-local space
    lines: list[LineString] = []
    for w in osm.ways:
        tags = w.get("tags") or {}
        hwy = tags.get("highway")
        if hwy not in _DECAL_WIDTHS:
            continue
        line_ll = way_line_ll(w, osm.nodes)
        if len(line_ll) < 2:
            continue
        lon, lat = zip(*line_ll)
        xs, ys = _LL_TO_ITM.transform(list(lon), list(lat))
        coords = [(x - cx_world, y - cy_world) for x, y in zip(xs, ys)
                  if abs(x - cx_world) <= half and abs(y - cy_world) <= half]
        if len(coords) >= 2:
            try:
                lines.append(LineString(coords))
            except Exception:
                pass
    if not lines:
        return []
    network = unary_union(lines)

    out: list[DecalRoad] = []
    seen_targets: set[tuple[float, float]] = set()
    # Stop the driveway THIS MUCH past the road centreline so the
    # driveway never overlaps the road decal (worst-case rural road
    # half-width ~3.5 m + a 1 m gap for the verge to read).
    ROAD_END_BACKOFF_M = 4.5
    for b in buildings:
        try:
            bp = Point(b.x_m, b.y_m)
        except Exception:
            continue
        if abs(b.x_m) > half or abs(b.y_m) > half:
            continue
        try:
            road_pt = nearest_points(network, bp)[0]
        except Exception:
            continue
        d = bp.distance(road_pt)
        # Need at least enough distance for the back-off + a small visible
        # driveway segment. Skip buildings that are essentially on top
        # of the road already.
        if d < (ROAD_END_BACKOFF_M + 6.0) or d > 350.0:
            continue
        key = (round(road_pt.x / 2.0), round(road_pt.y / 2.0))
        if key in seen_targets:
            continue
        seen_targets.add(key)
        dx = bp.x - road_pt.x; dy = bp.y - road_pt.y
        ln = (dx * dx + dy * dy) ** 0.5 or 1.0
        ux, uy = dx / ln, dy / ln          # unit vector road → building
        # Start point: just past the road EDGE, not on the centreline.
        start_x = road_pt.x + ux * ROAD_END_BACKOFF_M
        start_y = road_pt.y + uy * ROAD_END_BACKOFF_M
        # End point: 3 m short of building centroid so it doesn't poke
        # through the front wall.
        end_x = bp.x - ux * 3.0
        end_y = bp.y - uy * 3.0
        z0 = _z_at(heightmap_m, region.side_m, start_x, start_y) + 0.04
        z1 = _z_at(heightmap_m, region.side_m, end_x,   end_y)   + 0.04
        out.append(DecalRoad(
            osm_id=-(abs(int(b.osm_id)) % 1_000_000) - 1,
            name=f"drive_{abs(int(b.osm_id)) % 100000}",
            width_m=2.4,
            nodes_xyz=[(start_x, start_y, z0), (end_x, end_y, z1)],
            material="dirt",
        ))
    return out


# ---------------------------------------------------------------------------
# Procedural road decal texture (asphalt + dashed yellow line)
# ---------------------------------------------------------------------------
def write_road_decal_texture() -> Path:
    cache_dir = config.CACHE_DIR / "textures"
    cache_dir.mkdir(parents=True, exist_ok=True)
    out = cache_dir / "road_decal.png"
    if out.exists():
        return out
    w, h = 256, 1024  # narrow strip mapped along road length; wraps along V
    rng = np.random.default_rng(0xC00C5)
    base = rng.integers(45, 75, (h, w, 3), dtype=np.uint8)
    # Centre dashed yellow line
    cy = w // 2
    for y in range(0, h, 64):
        base[y:y + 32, cy - 2:cy + 2] = (220, 200, 60)
    # Edge solid white lines
    base[:, 18:21] = (220, 220, 220)
    base[:, w - 21:w - 18] = (220, 220, 220)
    Image.fromarray(base).save(out, optimize=True)
    return out


def write_drive_decal_texture() -> Path:
    """Brown gravel / dirt texture for driveways. Strip narrower than the
    road decal because driveways are typically ~2.5 m wide."""
    cache_dir = config.CACHE_DIR / "textures"
    cache_dir.mkdir(parents=True, exist_ok=True)
    out = cache_dir / "drive_decal.png"
    if out.exists():
        return out
    w, h = 128, 512
    rng = np.random.default_rng(0xD11A7)
    base = np.zeros((h, w, 3), dtype=np.uint8)
    # Brown noise base, slight greenish tint at edges for grass overgrowth
    base[..., 0] = rng.integers(95, 135, (h, w), dtype=np.uint8)   # R
    base[..., 1] = rng.integers(80, 110, (h, w), dtype=np.uint8)   # G
    base[..., 2] = rng.integers(55, 80, (h, w), dtype=np.uint8)    # B
    # Two darker tracks where wheels run
    base[:, 28:36] = base[:, 28:36] // 2 + 20
    base[:, w - 36:w - 28] = base[:, w - 36:w - 28] // 2 + 20
    Image.fromarray(base).save(out, optimize=True)
    return out

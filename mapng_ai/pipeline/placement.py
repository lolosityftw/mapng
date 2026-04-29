"""Stage 6 — object placement.

For Phase 3 this only deals with *buildings*. Foliage / water / decals are
deliberately deferred to Phase 5. Per spec §5.3:

  for each OSM building footprint:
    1. polygon → oriented bounding box (centroid, length, width, rotation)
    2. determine type from `building=*` tag, fall back to context
    3. determine height: `height` (m) > `building:levels`*3 m > heuristic
    4. seed = OSM way id (deterministic re-runs)
    5. ask asset provider for a model
    6. snap Z to terrain
    7. rotate to OBB orientation
    8. scale uniformly (clamped to ±40 %)
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

import numpy as np
from pyproj import Transformer
from shapely.geometry import Polygon

from mapng_ai.assets.base import AssetProvider, BuildingAsset
from mapng_ai.pipeline.region import Region
from mapng_ai.sources.overpass import OSMData, way_polygon_ll


_LL_TO_ITM = Transformer.from_crs("EPSG:4326", "EPSG:2157", always_xy=True)


@dataclass(frozen=True)
class BuildingPlacement:
    osm_id: int
    asset: BuildingAsset
    # Position is in BeamNG world space (centre of terrain = origin)
    x_m: float
    y_m: float
    z_m: float
    yaw_rad: float
    scale_xyz: tuple[float, float, float]


def _oriented_bbox(polygon: Polygon) -> tuple[float, float, float, float, float]:
    """(cx_m, cy_m, length_m, width_m, yaw_rad). Length aligned with the long side."""
    obb = polygon.minimum_rotated_rectangle
    coords = list(obb.exterior.coords)[:4]
    edges = [
        (coords[1][0] - coords[0][0], coords[1][1] - coords[0][1]),
        (coords[2][0] - coords[1][0], coords[2][1] - coords[1][1]),
    ]
    lengths = [(ex * ex + ey * ey) ** 0.5 for ex, ey in edges]
    if lengths[0] >= lengths[1]:
        long_edge = edges[0]; length, width = lengths[0], lengths[1]
    else:
        long_edge = edges[1]; length, width = lengths[1], lengths[0]
    yaw = float(np.arctan2(long_edge[1], long_edge[0]))
    cx, cy = polygon.centroid.x, polygon.centroid.y
    return cx, cy, length, width, yaw


def _infer_type(tags: dict) -> str:
    val = (tags.get("building") or "").lower()
    if val in ("yes", "true"):
        return "default"
    return val or "default"


def _infer_levels_and_height(tags: dict, footprint_m2: float) -> tuple[int, float]:
    if "height" in tags:
        try:
            h = float(str(tags["height"]).split()[0].replace("m", ""))
            return max(1, int(round(h / 3.0))), h
        except ValueError:
            pass
    if "building:levels" in tags:
        try:
            lv = max(1, int(float(tags["building:levels"])))
            return lv, lv * 3.0
        except ValueError:
            pass
    if footprint_m2 > 600:
        return 2, 7.0      # commercial / industrial guess
    return 1, 4.0


def _z_at(heightmap_m: np.ndarray, side_m: float, x_m: float, y_m: float) -> float:
    """Sample terrain height at (x, y) where origin is the terrain centre."""
    size = heightmap_m.shape[0]
    # Map x ∈ [-half, +half] → col ∈ [0, size-1], y likewise
    half = side_m / 2
    u = (x_m + half) / side_m
    v = 1.0 - (y_m + half) / side_m   # heightmap row 0 = north (image top)
    col = int(np.clip(round(u * (size - 1)), 0, size - 1))
    row = int(np.clip(round(v * (size - 1)), 0, size - 1))
    return float(heightmap_m[row, col])


def place_buildings(
    osm: OSMData,
    region: Region,
    heightmap_m: np.ndarray,
    provider: AssetProvider,
) -> list[BuildingPlacement]:
    placements: list[BuildingPlacement] = []
    cx_world = (region.working_itm.west + region.working_itm.east) / 2
    cy_world = (region.working_itm.south + region.working_itm.north) / 2
    half = region.side_m / 2

    for way in osm.ways:
        tags = way.get("tags") or {}
        if "building" not in tags:
            continue
        ring_ll = way_polygon_ll(way, osm.nodes)
        if ring_ll is None:
            continue

        # Reproject footprint to ITM, then shift so terrain centre is the origin
        lon, lat = zip(*ring_ll)
        xs, ys = _LL_TO_ITM.transform(list(lon), list(lat))
        xs_local = [x - cx_world for x in xs]
        ys_local = [y - cy_world for y in ys]
        try:
            poly = Polygon(zip(xs_local, ys_local))
            if not poly.is_valid or poly.area < 8.0:   # ignore < 8 m² blobs
                continue
        except Exception:
            continue

        # Reject footprints outside the terrain extent
        cx_local, cy_local, length, width, yaw = _oriented_bbox(poly)
        if abs(cx_local) > half or abs(cy_local) > half:
            continue

        btype = _infer_type(tags)
        levels, height = _infer_levels_and_height(tags, poly.area)
        seed = int(way["id"])
        asset = provider.get_building(
            footprint_m2=float(poly.area),
            levels=levels,
            building_type=btype,
            seed=seed,
        )
        nat_l, nat_w, nat_h = asset.natural_size_m

        # Per spec: scale uniformly, clamp to ±40 %
        target_scale = max(length / max(nat_l, 1e-3), width / max(nat_w, 1e-3))
        target_scale = max(0.6, min(1.4, target_scale))
        scale_xyz = (length, width, height)  # for the unit-cube box.dae we scale axis-by-axis
        z = _z_at(heightmap_m, region.side_m, cx_local, cy_local)
        placements.append(BuildingPlacement(
            osm_id=seed, asset=asset,
            x_m=cx_local, y_m=cy_local, z_m=z,
            yaw_rad=yaw, scale_xyz=scale_xyz,
        ))
    return placements

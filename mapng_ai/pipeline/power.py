"""Power-line pole extraction — OSM `power=line|minor_line` ways → telegraph
poles placed every ~40m along each line.

NI rural roads almost always have wooden distribution poles. OSM tags:
  - `power=line` — high-voltage transmission (rare, use sparingly)
  - `power=minor_line` — local distribution (the rural pole+wire we see everywhere)
  - `power=pole` — single pole (already a point, just place one)
  - `power=tower` — large transmission tower (skip — needs a different mesh)

We focus on `minor_line` as the dominant NI rural feature.

Output: list of `PolePlacement` objects → TSStatics in the export pipeline.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from pyproj import Transformer
from shapely.geometry import LineString, Point

from mapng_ai.pipeline.region import Region, sample_terrain_height as _z_at
from mapng_ai.sources.overpass import OSMData, way_line_ll


_LL_TO_ITM = Transformer.from_crs("EPSG:4326", "EPSG:2157", always_xy=True)


@dataclass(frozen=True)
class PolePlacement:
    x: float
    y: float
    z: float
    yaw: float          # rotates so the crossbar is perpendicular to the line
    height_m: float     # actual pole height in metres (typically 9m)


# Spacing along OSM power=minor_line ways. 40-50m is realistic for NI.
_POLE_SPACING_M = 45.0
_POLE_HEIGHT_M = 9.0          # natural pole height
_POLE_HEIGHT_TRANSMISSION_M = 14.0


def extract_power_poles(osm: OSMData, region: Region,
                       heightmap_m: np.ndarray) -> list[PolePlacement]:
    """Walk OSM power lines, drop a pole every ~45m. Also catches
    standalone `power=pole` nodes."""
    cx_world = (region.working_itm.west + region.working_itm.east) / 2
    cy_world = (region.working_itm.south + region.working_itm.north) / 2
    half = region.side_m / 2

    out: list[PolePlacement] = []

    # ----- Power lines (poles along them) -----
    for w in osm.ways:
        tags = w.get("tags") or {}
        ptype = tags.get("power")
        if ptype not in ("line", "minor_line"):
            continue
        is_transmission = (ptype == "line")
        line_ll = way_line_ll(w, osm.nodes)
        if not line_ll or len(line_ll) < 2:
            continue
        try:
            lon, lat = zip(*line_ll)
            xs, ys = _LL_TO_ITM.transform(list(lon), list(lat))
            xs_local = [x - cx_world for x in xs]
            ys_local = [y - cy_world for y in ys]
            line = LineString(zip(xs_local, ys_local))
            if line.is_empty or line.length < _POLE_SPACING_M * 0.4:
                continue
        except Exception:
            continue

        # Sample every spacing — endpoints + interior
        L = line.length
        # Always include both ends + at least one interior pole if long enough
        sample_distances = [0.0]
        d = _POLE_SPACING_M
        while d < L - 1.0:
            sample_distances.append(d)
            d += _POLE_SPACING_M
        sample_distances.append(L)

        for dist in sample_distances:
            pt = line.interpolate(dist)
            if abs(pt.x) > half or abs(pt.y) > half:
                continue
            # Tangent for crossbar yaw
            d2 = min(L, dist + 0.5)
            d1 = max(0.0, dist - 0.5)
            if d2 - d1 < 1e-6:
                yaw = 0.0
            else:
                p0 = line.interpolate(d1)
                p1 = line.interpolate(d2)
                # Crossbar perpendicular to the line tangent. The pole DAE
                # has crossbar along the X axis, so yaw the pole 90° from
                # the line so crossbar is perpendicular.
                tangent_yaw = math.atan2(p1.y - p0.y, p1.x - p0.x)
                yaw = tangent_yaw  # pole crossbar already perpendicular
                                   # (the +X crossbar rotates WITH the line direction)
            z = _z_at(heightmap_m, region.side_m, pt.x, pt.y)
            out.append(PolePlacement(
                x=pt.x, y=pt.y, z=z, yaw=yaw,
                height_m=_POLE_HEIGHT_TRANSMISSION_M if is_transmission else _POLE_HEIGHT_M,
            ))

    # ----- Standalone power=pole nodes -----
    nodes_by_id = {n["id"]: n for n in osm.nodes}
    for n in osm.nodes:
        tags = n.get("tags") or {}
        if tags.get("power") != "pole":
            continue
        try:
            x, y = _LL_TO_ITM.transform(n["lon"], n["lat"])
            x -= cx_world; y -= cy_world
            if abs(x) > half or abs(y) > half:
                continue
            z = _z_at(heightmap_m, region.side_m, x, y)
            out.append(PolePlacement(
                x=x, y=y, z=z, yaw=0.0,
                height_m=_POLE_HEIGHT_M,
            ))
        except Exception:
            continue
    return out


def pole_tsstatic_dicts(poles, level_name: str) -> list[dict]:
    """Convert PolePlacements to TSStatic items.level.json entries."""
    out: list[dict] = []
    shape = f"/levels/{level_name}/art/shapes/infrastructure/pole.dae"
    for i, p in enumerate(poles):
        c, s = math.cos(p.yaw), math.sin(p.yaw)
        # Pole DAE is 0.95 units tall — scale_z = height/0.95.
        # Width/depth scale = 1.0 (pole stays thin at its natural width).
        sz = p.height_m / 0.95
        out.append({
            "class": "TSStatic",
            "name": f"pole_{i}",
            "shapeName": shape,
            "position": [p.x, p.y, p.z],
            "rotationMatrix": [c, -s, 0, s, c, 0, 0, 0, 1],
            "scale": [1.0, 1.0, sz],
            "useInstanceRenderData": True,
        })
    return out

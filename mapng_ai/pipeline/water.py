"""Water feature extraction — OSM natural=water polygons → BeamNG WaterBlocks.

Reference: vanilla Italy uses WaterBlocks (not WaterPlanes) — one per
distinct water body. Each WaterBlock has full visual + physics params
(foam, ripples, depth gradient). We clone Italy's settings and reference
its texture paths via the VFS so we don't need to bundle anything.

A WaterBlock is positioned at the polygon's bounding-box centre. Its
scale is the bbox dimensions. The z-coord is the terrain elevation at
the centre (with a small buffer down so the water "floods" properly
into low ground).
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from pyproj import Transformer
from shapely.geometry import Polygon

from mapng_ai.pipeline.region import Region, sample_terrain_height as _z_at
from mapng_ai.sources.overpass import OSMData, way_polygon_ll


_LL_TO_ITM = Transformer.from_crs("EPSG:4326", "EPSG:2157", always_xy=True)


@dataclass(frozen=True)
class WaterBody:
    """One BeamNG WaterBlock — a localised body of water."""
    name: str
    cx: float
    cy: float
    cz: float          # surface elevation (world z)
    length: float      # bbox length
    width: float       # bbox width
    depth: float       # how deep the water volume extends below surface
    yaw: float         # rotation (rad)


_WATER_TAGS = {
    ("natural", "water"),
    ("waterway", "riverbank"),
    ("landuse", "reservoir"),
    ("landuse", "basin"),
}


# Sea-level z used for coastline-bounded ocean WaterBlocks. NI's coast is
# approximately at OS Newlyn datum (≈0m). For maps inland that include
# part of the coast, this gives us an actual ocean visible.
_SEA_LEVEL_M = 0.0


def extract_water_bodies(osm: OSMData, region: Region,
                        heightmap_m: np.ndarray) -> list[WaterBody]:
    """Find OSM water polygons and return one WaterBody per polygon."""
    cx_world = (region.working_itm.west + region.working_itm.east) / 2
    cy_world = (region.working_itm.south + region.working_itm.north) / 2
    half = region.side_m / 2

    bodies: list[WaterBody] = []
    for w in osm.ways:
        tags = w.get("tags") or {}
        if not any(tags.get(k) == v for (k, v) in _WATER_TAGS):
            continue
        ring_ll = way_polygon_ll(w, osm.nodes)
        if ring_ll is None:
            continue
        try:
            lon, lat = zip(*ring_ll)
            xs, ys = _LL_TO_ITM.transform(list(lon), list(lat))
            xs_local = [x - cx_world for x in xs]
            ys_local = [y - cy_world for y in ys]
            poly = Polygon(zip(xs_local, ys_local))
            if not poly.is_valid:
                poly = poly.buffer(0)
            if poly.is_empty or poly.area < 50.0:   # skip <50 m² puddles
                continue
        except Exception:
            continue

        # Bbox in terrain-local coords
        minx, miny, maxx, maxy = poly.bounds
        if (maxx < -half or minx > half or
                maxy < -half or miny > half):
            continue
        cx = float((minx + maxx) / 2)
        cy = float((miny + maxy) / 2)
        length = float(maxx - minx)
        width  = float(maxy - miny)

        # Surface elevation: average terrain height at the polygon centre
        # minus a small dip so water "fills in" low ground naturally.
        cz = _z_at(heightmap_m, region.side_m, cx, cy) - 0.20
        depth = max(2.0, min(8.0, max(length, width) * 0.04))
        bodies.append(WaterBody(
            name=tags.get("name", f"water_{w['id']}"),
            cx=cx, cy=cy, cz=cz,
            length=length, width=width, depth=depth, yaw=0.0,
        ))
    return bodies


def extract_coastline(osm: OSMData, region: Region) -> WaterBody | None:
    """If the bbox includes any OSM coastline, emit one big WaterBody for
    the sea side. The coastline way separates land from sea — we don't
    know which side is which without OSM convention parsing, so we emit
    a WaterBlock at sea level covering the full terrain bounds. The
    terrain itself stays unchanged (no carving); only land that's above
    sea level shows.

    Returns None if no coastline tags are present.
    """
    has_coast = False
    for w in osm.ways:
        tags = w.get("tags") or {}
        if tags.get("natural") == "coastline":
            has_coast = True
            break
    if not has_coast:
        return None
    side = float(region.side_m)
    return WaterBody(
        name="ocean",
        cx=0.0, cy=0.0, cz=_SEA_LEVEL_M,
        # Make it generous — extends 50m beyond terrain edge so the
        # horizon doesn't show a hard water/sky seam at the bbox edge.
        length=side + 100.0,
        width=side + 100.0,
        depth=20.0,
        yaw=0.0,
    )


def water_block_dict(body: WaterBody, level_name: str, idx: int) -> dict:
    """Return the BeamNG WaterBlock items.level.json entry for a WaterBody.

    Field set is cloned from vanilla Italy's quarry-water setup. Texture
    paths point into Italy's VFS — we don't ship any water textures.
    """
    cos_y = float(np.cos(body.yaw))
    sin_y = float(np.sin(body.yaw))
    safe_name = "".join(c if c.isalnum() else "_" for c in body.name)[:48]
    return {
        "name": f"water_{idx}_{safe_name}",
        "class": "WaterBlock",
        "position": [body.cx, body.cy, body.cz],
        "scale": [body.length, body.width, body.depth],
        "rotationMatrix": [cos_y, sin_y, 0, -sin_y, cos_y, 0, 0, 0, 1],
        # Visual: muddy-blue rural water (Italy's quarry preset is too clear)
        "baseColor": [115, 142, 132, 255],
        "depthGradientTex": "/levels/italy/art/water/depthcolor_ramp_italy_muddy.png",
        "depthGradientMax": 30,
        "foamTex":   "levels/italy/art/water/foam2.dds",
        "rippleTex": "/levels/italy/art/water/ripple.dds",
        # Physics
        "fresnelBias":  0.2,
        "fresnelPower": 20,
        "fullReflect":  False,
        "reflectivity": 0.6,
        "specularPower": 200,
        "waterFogDensity": 1,
        "waterFogDensityOffset": 0.1,
        "wetDarkening": 0.5,
        "wetDepth": 0.2,
        "gridSize": 1, "gridElementSize": 1,
        "overallRippleMagnitude": 0.2,
        "overallWaveMagnitude": 0,
        # Three undulation directions for natural look
        "Waves (vertex undulation)": [
            {"waveDir": [0, 1],     "waveMagnitude": 0.20, "waveSpeed": 1},
            {"waveDir": [0.707, 0.707], "waveMagnitude": 0.20, "waveSpeed": 1},
            {"waveDir": [0.5, 0.86], "waveMagnitude": 0.20, "waveSpeed": 1},
        ],
        "Ripples (texture animation)": [
            {"rippleDir": [0, 1],     "rippleMagnitude": 0.8,  "rippleSpeed": 0.001, "rippleTexScale": [12, 12]},
            {"rippleDir": [0, 1],     "rippleMagnitude": None, "rippleSpeed": 0.02,  "rippleTexScale": [6, 6]},
            {"rippleDir": [0.7, -0.7], "rippleMagnitude": 1,    "rippleSpeed": 0.02,  "rippleTexScale": [3, 3]},
        ],
        "Foam": [{}, {}],
        "foamAmbientLerp": 1.3,
        "foamMaxDepth": 0.15,
        "foamRippleInfluence": 0.015,
    }

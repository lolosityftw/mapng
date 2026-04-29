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
from mapng_ai.pipeline.region import Region
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


def _z_at(heightmap_m: np.ndarray, side_m: float, x_m: float, y_m: float) -> float:
    size = heightmap_m.shape[0]
    half = side_m / 2
    u = np.clip((x_m + half) / side_m, 0, 1)
    v = np.clip(1.0 - (y_m + half) / side_m, 0, 1)
    col = int(round(u * (size - 1)))
    row = int(round(v * (size - 1)))
    return float(heightmap_m[row, col])


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
        out.append(DecalRoad(
            osm_id=int(w["id"]),
            name=tags.get("name", "road"),
            width_m=_DECAL_WIDTHS[hwy],
            nodes_xyz=nodes,
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

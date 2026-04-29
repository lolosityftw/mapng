"""Stage 4 — land-cover classification (Phase 4 MVP).

Per spec §4.1 the long-term plan is a pretrained aerial-segmentation model.
For the MVP we deterministically *rasterise OSM* features into the class map.
This delivers honest results with no GPU dependency — and the AI seg model
can later replace this whole module behind the same `class_map` output.

Output: a (size, size) uint8 array of class IDs, terrain space (row 0 = NORTH).

NI rural backgrounds default to class 3 (rough pasture). Roads always win.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

import numpy as np
from pyproj import Transformer
from rasterio import features
from rasterio.transform import from_bounds
from shapely.geometry import LineString, Polygon, mapping
from shapely.ops import transform

from mapng_ai.pipeline.region import Region
from mapng_ai.sources.overpass import OSMData, way_line_ll, way_polygon_ll


_LL_TO_ITM = Transformer.from_crs("EPSG:4326", "EPSG:2157", always_xy=True)


# ---------------------------------------------------------------------------
# Class table (spec §4.1) — at most 8 reach the .ter (BeamNG limit)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class LandClass:
    id: int
    key: str
    label: str
    color_rgb: tuple[int, int, int]


CLASSES: dict[int, LandClass] = {
    0: LandClass(0, "asphalt",   "Asphalt road",   (58,  58,  58)),
    1: LandClass(1, "concrete",  "Concrete",       (170, 170, 170)),
    2: LandClass(2, "lawn",      "Short grass",    (95,  140, 60)),
    3: LandClass(3, "pasture",   "Rough pasture",  (110, 138, 77)),
    4: LandClass(4, "earth",     "Bare earth",     (139, 111, 71)),
    5: LandClass(5, "gravel",    "Gravel track",   (125, 116, 102)),
    6: LandClass(6, "water",     "Water",          (58,  96,  144)),
    7: LandClass(7, "forest",    "Forest floor",   (58,  90,  37)),
}

DEFAULT_CLASS = 3   # rural NI background = rough pasture


# OSM tag → class ID. Listed roughly low-to-high priority (later overrides earlier).
_LANDUSE_MAP: dict[tuple[str, str], int] = {
    ("landuse", "forest"):    7,
    ("natural", "wood"):      7,
    ("natural", "scrub"):     3,
    ("natural", "heath"):     3,
    ("natural", "grassland"): 2,
    ("leisure", "park"):      2,
    ("leisure", "garden"):    2,
    ("landuse", "grass"):     2,
    ("landuse", "meadow"):    3,
    ("landuse", "farmland"):  3,
    ("landuse", "farmyard"):  4,
    ("landuse", "residential"): 1,
    ("landuse", "commercial"):  1,
    ("landuse", "industrial"):  1,
    ("landuse", "retail"):      1,
    ("landuse", "construction"): 4,
    ("landuse", "quarry"):    4,
    ("natural", "bare_rock"): 4,   # rock would be id 8 if we had a slot
    ("natural", "water"):     6,
    ("landuse", "basin"):     6,
    ("landuse", "reservoir"): 6,
}

# Highway class → (target class, half-width metres)
_ROAD_WIDTHS: dict[str, tuple[int, float]] = {
    "motorway":     (0, 7.0),
    "trunk":        (0, 6.0),
    "primary":      (0, 5.0),
    "secondary":    (0, 4.5),
    "tertiary":     (0, 4.0),
    "unclassified": (0, 3.5),
    "residential":  (0, 3.0),
    "service":      (0, 2.5),
    "track":        (5, 2.0),
    "path":         (5, 1.0),
    "footway":      (5, 1.0),
    "cycleway":     (5, 1.5),
    "pedestrian":   (1, 2.5),
    "living_street": (0, 3.0),
}


# ---------------------------------------------------------------------------
# Geometry transforms
# ---------------------------------------------------------------------------
def _to_local(geom, cx_world: float, cy_world: float):
    """ITM → terrain-local (centre at origin) — transformed in-place."""
    return transform(lambda x, y, z=None: (x - cx_world, y - cy_world), geom)


def _polygon_local(ring_ll, cx, cy) -> Polygon | None:
    lon, lat = zip(*ring_ll)
    xs, ys = _LL_TO_ITM.transform(list(lon), list(lat))
    poly = Polygon(zip(xs, ys))
    if not poly.is_valid:
        poly = poly.buffer(0)
    if poly.is_empty:
        return None
    return _to_local(poly, cx, cy)


def _line_buffer_local(line_ll, cx, cy, half_width_m) -> Polygon | None:
    if len(line_ll) < 2:
        return None
    lon, lat = zip(*line_ll)
    xs, ys = _LL_TO_ITM.transform(list(lon), list(lat))
    line = LineString(zip(xs, ys))
    poly = line.buffer(half_width_m, cap_style=2, join_style=2)
    return _to_local(poly, cx, cy) if poly and not poly.is_empty else None


# ---------------------------------------------------------------------------
# Rasterisation
# ---------------------------------------------------------------------------
def build_class_map(osm: OSMData, region: Region, size: int) -> np.ndarray:
    cx_world = (region.working_itm.west + region.working_itm.east) / 2
    cy_world = (region.working_itm.south + region.working_itm.north) / 2
    half = region.side_m / 2

    # rasterio transform: outer bounds in local coords, row 0 = north
    transform_aff = from_bounds(-half, -half, half, half, size, size)

    # Start everything as the default (pasture)
    class_map = np.full((size, size), DEFAULT_CLASS, dtype=np.uint8)

    SENTINEL = 255  # used as 'untouched' so class 0 (asphalt) survives

    def _rasterise(shapes_with_ids: Iterable[tuple[Polygon, int]]) -> None:
        nonlocal class_map
        shapes = [(mapping(g), cid + 1) for g, cid in shapes_with_ids if g is not None]
        if not shapes:
            return
        rast = features.rasterize(
            shapes=shapes,
            out_shape=(size, size),
            transform=transform_aff,
            fill=0,
            dtype=np.uint8,
            all_touched=False,
        )
        # rast > 0 means *any* class wrote here; subtract 1 to recover the real id
        mask = rast > 0
        class_map[mask] = rast[mask] - 1

    # 1) Landuse / natural polygons (lowest priority)
    landuse_shapes = []
    for w in osm.ways:
        tags = w.get("tags") or {}
        for (k, v), cid in _LANDUSE_MAP.items():
            if tags.get(k) == v:
                ring = way_polygon_ll(w, osm.nodes)
                if ring is None:
                    continue
                poly = _polygon_local(ring, cx_world, cy_world)
                if poly is not None:
                    landuse_shapes.append((poly, cid))
                break
    _rasterise(landuse_shapes)

    # 2) Waterways (rivers/streams as buffered lines)
    water_shapes = []
    for w in osm.ways:
        tags = w.get("tags") or {}
        wtype = tags.get("waterway")
        if wtype in ("river", "stream", "canal", "drain", "ditch"):
            line_ll = way_line_ll(w, osm.nodes)
            half_w = {"river": 5.0, "canal": 4.0, "stream": 1.0, "drain": 0.5, "ditch": 0.5}[wtype]
            poly = _line_buffer_local(line_ll, cx_world, cy_world, half_w)
            if poly is not None:
                water_shapes.append((poly, 6))
    _rasterise(water_shapes)

    # 3) Roads (highest priority, painted on top)
    road_shapes = []
    for w in osm.ways:
        tags = w.get("tags") or {}
        hwy = tags.get("highway")
        if not hwy or hwy not in _ROAD_WIDTHS:
            continue
        cid, half_w = _ROAD_WIDTHS[hwy]
        line_ll = way_line_ll(w, osm.nodes)
        poly = _line_buffer_local(line_ll, cx_world, cy_world, half_w)
        if poly is not None:
            road_shapes.append((poly, cid))
    _rasterise(road_shapes)

    return class_map

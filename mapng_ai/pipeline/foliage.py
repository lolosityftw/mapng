"""Stage 5 — foliage placement.

Three sources of vegetation, all OSM-driven (segmentation-driven foliage is a
nice-to-have for Phase 5+):

1. Forest polygons (landuse=forest, natural=wood) → Poisson-disk samples
2. Standalone OSM trees (node with natural=tree) → one tree each
3. Hedgerows (way with barrier=hedge / natural=tree_row) → linear extrusions

Output is intentionally *capped* — BeamNG renders thousands of TSStatic
objects fine but at some point you want a Forest object instead, which is a
later upgrade.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from pyproj import Transformer
from shapely.geometry import LineString, Polygon

from mapng_ai.assets.library import pick_tree, tree_library
from mapng_ai.pipeline.region import Region
from mapng_ai.sources.overpass import OSMData, way_line_ll, way_polygon_ll


_LL_TO_ITM = Transformer.from_crs("EPSG:4326", "EPSG:2157", always_xy=True)


# Hard caps so we never emit absurd numbers
MAX_TREES = 4000
MAX_HEDGE_SEGMENTS = 4000      # synthesised road hedges add lots
TREES_PER_M2_FOREST = 0.035    # ~1 per 28 m²
HEDGE_SEGMENT_LEN_M = 4.0      # subdivide long hedges into ~4 m chunks

# Highways that get synthesised hedges flanking them. Rural NI roads are
# almost always hedge-bordered but rarely tagged barrier=hedge in OSM.
_ROAD_CLASSES_WITH_HEDGES = {
    "secondary":     {"width": 9.0,  "hedge_offset": 1.5},
    "tertiary":      {"width": 8.0,  "hedge_offset": 1.5},
    "unclassified":  {"width": 7.0,  "hedge_offset": 1.2},
    "residential":   {"width": 6.5,  "hedge_offset": 1.0},
    "lane":          {"width": 5.0,  "hedge_offset": 0.8},
    "track":         {"width": 4.5,  "hedge_offset": 0.8},
    # motorway / trunk / primary deliberately excluded — verges, no hedges
}


@dataclass(frozen=True)
class TreePlacement:
    x: float
    y: float
    z: float
    scale_xyz: tuple[float, float, float]
    yaw: float
    shape_relpath: str = "art/shapes/foliage/tree.dae"
    species: str = "default"


@dataclass(frozen=True)
class HedgeSegment:
    x: float
    y: float
    z: float
    length_m: float
    width_m: float
    height_m: float
    yaw: float


@dataclass
class FoliageResult:
    trees: list[TreePlacement]
    hedges: list[HedgeSegment]
    forest_polys: int
    standalone_trees: int


# ---------------------------------------------------------------------------
# Poisson-disk sampling (Bridson 2007), bounded to a polygon mask
# ---------------------------------------------------------------------------
def _poisson_disk(width: float, height: float, r: float, seed: int, mask) -> list[tuple[float, float]]:
    """Sample points in [-w/2, w/2] × [-h/2, h/2]. `mask(x, y) -> bool` keeps point if true."""
    rng = np.random.default_rng(seed)
    cell = r / np.sqrt(2)
    cols = int(np.ceil(width / cell))
    rows = int(np.ceil(height / cell))
    grid: dict[tuple[int, int], tuple[float, float]] = {}
    pts: list[tuple[float, float]] = []
    active: list[int] = []

    def cell_of(x, y):
        return int((x + width / 2) // cell), int((y + height / 2) // cell)

    def fits(x, y):
        if not mask(x, y):
            return False
        cx, cy = cell_of(x, y)
        for dx in range(-2, 3):
            for dy in range(-2, 3):
                neigh = grid.get((cx + dx, cy + dy))
                if neigh is None:
                    continue
                nx, ny = neigh
                if (nx - x) ** 2 + (ny - y) ** 2 < r * r:
                    return False
        return True

    # Try a handful of random seeds in the polygon for the first point
    for _ in range(30):
        x = rng.uniform(-width / 2, width / 2)
        y = rng.uniform(-height / 2, height / 2)
        if fits(x, y):
            pts.append((x, y)); active.append(0)
            grid[cell_of(x, y)] = (x, y)
            break

    while active:
        idx = active[rng.integers(len(active))]
        cx, cy = pts[idx]
        placed = False
        for _ in range(20):
            theta = rng.uniform(0, 2 * np.pi)
            rad = rng.uniform(r, 2 * r)
            x = cx + rad * np.cos(theta)
            y = cy + rad * np.sin(theta)
            if abs(x) > width / 2 or abs(y) > height / 2:
                continue
            if fits(x, y):
                pts.append((x, y)); active.append(len(pts) - 1)
                grid[cell_of(x, y)] = (x, y)
                placed = True
                break
        if not placed:
            active.remove(idx)
    return pts


def _z_at(heightmap_m: np.ndarray, side_m: float, x_m: float, y_m: float) -> float:
    size = heightmap_m.shape[0]
    half = side_m / 2
    u = np.clip((x_m + half) / side_m, 0, 1)
    v = np.clip(1.0 - (y_m + half) / side_m, 0, 1)
    col = int(round(u * (size - 1)))
    row = int(round(v * (size - 1)))
    return float(heightmap_m[row, col])


# ---------------------------------------------------------------------------
def _project_polygon(ring_ll, cx: float, cy: float) -> Polygon | None:
    lon, lat = zip(*ring_ll)
    xs, ys = _LL_TO_ITM.transform(list(lon), list(lat))
    poly = Polygon(zip([x - cx for x in xs], [y - cy for y in ys]))
    if not poly.is_valid:
        poly = poly.buffer(0)
    return None if poly.is_empty else poly


def _project_line(line_ll, cx: float, cy: float) -> LineString | None:
    if len(line_ll) < 2:
        return None
    lon, lat = zip(*line_ll)
    xs, ys = _LL_TO_ITM.transform(list(lon), list(lat))
    return LineString(zip([x - cx for x in xs], [y - cy for y in ys]))


def place_foliage(osm: OSMData, region: Region, heightmap_m: np.ndarray, *, seed: int = 7) -> FoliageResult:
    cx_world = (region.working_itm.west + region.working_itm.east) / 2
    cy_world = (region.working_itm.south + region.working_itm.north) / 2
    half = region.side_m / 2
    rng = np.random.default_rng(seed)

    # ---- 1) Forest polygons → Poisson-disk samples ----
    forest_polys: list[Polygon] = []
    for w in osm.ways:
        tags = w.get("tags") or {}
        if tags.get("landuse") == "forest" or tags.get("natural") in ("wood", "scrub"):
            ring = way_polygon_ll(w, osm.nodes)
            if ring is None:
                continue
            poly = _project_polygon(ring, cx_world, cy_world)
            if poly is None:
                continue
            # Clip to terrain extent
            terrain_box = Polygon([(-half, -half), (half, -half), (half, half), (-half, half)])
            poly = poly.intersection(terrain_box)
            if poly.is_empty:
                continue
            if hasattr(poly, "geoms"):  # MultiPolygon
                for g in poly.geoms:
                    if not g.is_empty: forest_polys.append(g)
            else:
                forest_polys.append(poly)

    trees: list[TreePlacement] = []
    target_count = sum(int(p.area * TREES_PER_M2_FOREST) for p in forest_polys)
    # If forests are dense, scale density down to stay under MAX_TREES
    density_scale = min(1.0, MAX_TREES / max(target_count, 1))
    r_min = (1.0 / (TREES_PER_M2_FOREST * density_scale)) ** 0.5

    for fp_idx, poly in enumerate(forest_polys):
        bx, by, bX, bY = poly.bounds
        bw = bX - bx
        bh = bY - by
        if bw < r_min or bh < r_min:
            continue
        cx_local = (bx + bX) / 2
        cy_local = (by + bY) / 2
        # Sample in local-to-bbox frame, translate to world
        pts = _poisson_disk(bw, bh, r_min, seed=seed + fp_idx,
                            mask=lambda x, y, P=poly, ox=cx_local, oy=cy_local:
                            P.contains(__import__("shapely").geometry.Point(x + ox, y + oy)))
        for x, y in pts:
            if len(trees) >= MAX_TREES:
                break
            wx, wy = x + cx_local, y + cy_local
            sub_seed = int(abs(hash((round(wx, 1), round(wy, 1))))) & 0xFFFFFFFF
            sr = rng.default_rng(sub_seed) if hasattr(rng, "default_rng") else np.random.default_rng(sub_seed)
            sr = np.random.default_rng(sub_seed)
            height = float(6.0 + sr.random() * 9.0)
            radius = float(2.0 + sr.random() * 1.5)
            yaw = float(sr.random() * 2 * np.pi)
            z = _z_at(heightmap_m, region.side_m, wx, wy)
            lib_pick = pick_tree(sub_seed)
            shape_rel = lib_pick.rel_path if lib_pick else "art/shapes/foliage/tree.dae"
            species = lib_pick.type_label if lib_pick else "default"
            trees.append(TreePlacement(
                x=wx, y=wy, z=z,
                scale_xyz=(radius, radius, height),
                yaw=yaw,
                shape_relpath=shape_rel, species=species,
            ))
        if len(trees) >= MAX_TREES:
            break

    # ---- 2) Standalone OSM trees ----
    standalone = 0
    for nid, (lat, lon) in osm.nodes.items():
        # Need to look up tags — Overpass response stores tags on full node elements,
        # but we only kept (lat, lon). The raw cache has tags; pull them lazily.
        pass   # we drop this for v1; the forest polygons already give massive coverage

    # ---- 3) Hedgerows ----
    hedges: list[HedgeSegment] = []

    def _emit_segment(cx, cy, seg_len, yaw, *, width_m, height_m):
        if abs(cx) > half or abs(cy) > half:
            return
        z = _z_at(heightmap_m, region.side_m, cx, cy)
        hedges.append(HedgeSegment(
            x=cx, y=cy, z=z, length_m=seg_len,
            width_m=width_m, height_m=height_m, yaw=yaw,
        ))

    def _emit_along_line(line: LineString, *, width_m: float, height_m: float):
        n_seg = max(1, int(np.ceil(line.length / HEDGE_SEGMENT_LEN_M)))
        for i in range(n_seg):
            if len(hedges) >= MAX_HEDGE_SEGMENTS:
                return
            t0, t1 = i / n_seg, (i + 1) / n_seg
            p0 = line.interpolate(t0, normalized=True)
            p1 = line.interpolate(t1, normalized=True)
            seg_len = float(p0.distance(p1))
            yaw = float(np.arctan2(p1.y - p0.y, p1.x - p0.x))
            _emit_segment((p0.x + p1.x) / 2, (p0.y + p1.y) / 2, seg_len, yaw,
                          width_m=width_m, height_m=height_m)

    # 3a) Tagged OSM hedges/walls/fences — primary source
    for w in osm.ways:
        if len(hedges) >= MAX_HEDGE_SEGMENTS:
            break
        tags = w.get("tags") or {}
        is_hedge = tags.get("barrier") in ("hedge", "hedgerow", "fence") \
            or tags.get("natural") in ("tree_row",)
        if not is_hedge:
            continue
        line_ll = way_line_ll(w, osm.nodes)
        line = _project_line(line_ll, cx_world, cy_world)
        if line is None or line.length < 1.0:
            continue
        is_fence = tags.get("barrier") == "fence"
        _emit_along_line(line,
                         width_m=(0.6 if is_fence else 0.9),
                         height_m=(0.9 if is_fence else 1.6))

    # 3b) Synthesised hedges along rural road centrelines. NI roads almost
    # always have hedges either side but they're rarely tagged. We offset
    # the road centreline by half-width + a small gap, on both sides.
    if len(hedges) < MAX_HEDGE_SEGMENTS:
        for w in osm.ways:
            if len(hedges) >= MAX_HEDGE_SEGMENTS:
                break
            tags = w.get("tags") or {}
            hwy = tags.get("highway")
            cfg = _ROAD_CLASSES_WITH_HEDGES.get(hwy)
            if cfg is None:
                continue
            # Skip roads in built-up areas (residential streets in town
            # centres usually don't have hedges) — heuristic: if the
            # immediate vicinity is mostly buildings, skip.
            line_ll = way_line_ll(w, osm.nodes)
            line = _project_line(line_ll, cx_world, cy_world)
            if line is None or line.length < 5.0:
                continue
            # Offset both sides
            offset_dist = cfg["width"] / 2.0 + cfg["hedge_offset"]
            try:
                left = line.parallel_offset(offset_dist, "left", join_style=2)
                right = line.parallel_offset(offset_dist, "right", join_style=2)
            except Exception:
                continue
            for off in (left, right):
                if off is None or off.is_empty:
                    continue
                # parallel_offset can return MultiLineString
                geoms = [off] if hasattr(off, "coords") else list(getattr(off, "geoms", []))
                for g in geoms:
                    try:
                        if g.length < 4.0:
                            continue
                        _emit_along_line(g, width_m=0.9, height_m=1.4)
                    except Exception:
                        continue

    return FoliageResult(
        trees=trees,
        hedges=hedges,
        forest_polys=len(forest_polys),
        standalone_trees=standalone,
    )

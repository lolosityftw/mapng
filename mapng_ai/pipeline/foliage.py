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
from shapely.geometry import LineString, MultiLineString, Point, Polygon
from shapely.ops import unary_union

from mapng_ai.assets.library import pick_tree, tree_library
from mapng_ai.pipeline.region import Region, sample_terrain_height
from mapng_ai.sources.overpass import OSMData, way_line_ll, way_polygon_ll


_LL_TO_ITM = Transformer.from_crs("EPSG:4326", "EPSG:2157", always_xy=True)


# Hard caps — tripled from previous values for better Orritor-Rd-style
# rural density. Low-poly TSStatic count budget at these settings:
#   12000 trees × 32 tris  + 16000 hedges × 12 tris + 2000 bushes × 8 tris
#   ≈ 600k tris total — well within BeamNG's render budget.
MAX_TREES = 12000
MAX_HEDGE_SEGMENTS = 16000     # synthesised road hedges add lots
TREES_PER_M2_FOREST = 0.05     # denser forests (was 0.035) — 1 per 20 m²
HEDGE_SEGMENT_LEN_M = 4.0      # subdivide long hedges into ~4 m chunks

# Highways that get synthesised hedges flanking them. Rural NI roads are
# almost always hedge-bordered but rarely tagged barrier=hedge in OSM.
_ROAD_CLASSES_WITH_HEDGES = {
    "primary":       {"width": 11.0, "hedge_offset": 2.0},  # rural primaries do have hedges
    "secondary":     {"width": 9.0,  "hedge_offset": 1.5},
    "tertiary":      {"width": 8.0,  "hedge_offset": 1.5},
    "unclassified":  {"width": 7.0,  "hedge_offset": 1.2},
    "residential":   {"width": 6.5,  "hedge_offset": 1.0},
    "living_street": {"width": 5.5,  "hedge_offset": 1.0},
    "lane":          {"width": 5.0,  "hedge_offset": 0.8},
    "track":         {"width": 4.5,  "hedge_offset": 0.8},
    "path":          {"width": 2.0,  "hedge_offset": 0.5},
    # 'service' (driveways) deliberately excluded — they shouldn't have
    # hedges flanking their entire length and we want to leave the
    # driveway texture unobstructed where it meets the main road.
    # motorway / trunk also excluded — they have grass verges, no hedges.
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
    # 'hedge' = green leafy bush cluster (default), 'wall' = drystone wall.
    # NI uses both interchangeably as field boundaries; tagged OSM
    # `barrier=wall|stone_wall|drystone_wall` flips this, plus ~25% of
    # synthesised field-boundary segments are randomised to walls so a
    # mapped area reads as a believable mix.
    material: str = "hedge"


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


# Local alias — the canonical implementation lives in region.py so all
# placement stages sample heights with identical semantics.
_z_at = sample_terrain_height


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


def add_garden_features(*, hedges: list, buildings, region: Region,
                        heightmap_m: np.ndarray, seed: int = 11) -> None:
    """Add a low garden wall + a small back-garden shed for residential
    buildings, in-place into the hedges list (sheds emitted as a special
    `material='gate'` placeholder won't pollute the field synthesiser).

    Operates on placed BuildingPlacements so we get the correct OBB
    yaw + dimensions for free. We treat anything with type_label
    matching residential / house / cottage as a candidate.
    """
    half = region.side_m / 2
    rng = np.random.default_rng(seed)
    RESIDENTIAL_TYPES = {"residential", "house", "detached", "semi", "bungalow",
                         "apartment", "cottage", "default"}
    for b in buildings:
        if (b.asset.type_label or "").lower() not in RESIDENTIAL_TYPES:
            continue
        sl, sw, sh = b.scale_xyz
        # Per-building deterministic dice — only ~70% of houses get the
        # garden treatment so the world doesn't look uniform.
        if rng.random() > 0.70:
            continue
        # Garden extent — keep tight to the house so walls don't poke
        # through neighbouring buildings in tight terraced clusters.
        # +4m on long axis (back garden), +3m on each side.
        garden_l = sl + 4.0
        garden_w = sw + 3.0
        cos_y = float(np.cos(b.yaw_rad))
        sin_y = float(np.sin(b.yaw_rad))
        # 4 wall segments forming a rectangle around the house (skipping
        # the front so the driveway can enter). Front = +half_long axis.
        for side, dx, dy, length, w_yaw in [
            ("back",  -garden_l / 2, 0,           garden_w, b.yaw_rad + np.pi / 2),
            ("left",   0,           +garden_w / 2, garden_l, b.yaw_rad),
            ("right",  0,           -garden_w / 2, garden_l, b.yaw_rad),
        ]:
            # Rotate offset by yaw and add to building centre
            world_dx = cos_y * dx - sin_y * dy
            world_dy = sin_y * dx + cos_y * dy
            wx = b.x_m + world_dx
            wy = b.y_m + world_dy
            if abs(wx) > half or abs(wy) > half:
                continue
            z = _z_at(heightmap_m, region.side_m, wx, wy) + 0.05
            # Use HedgeSegment as the carrier even for walls — same
            # rendering plumbing already exists.
            hedges.append(HedgeSegment(
                x=wx, y=wy, z=z, length_m=length,
                width_m=0.3, height_m=0.9, yaw=w_yaw, material="wall",
            ))
        # Back-garden shed — pick a spot ~5 m behind the house.
        shed_dx = -(sl / 2 + 5.0)
        shed_dy = (rng.uniform(-1.0, 1.0)) * (sw / 4)
        wx = b.x_m + cos_y * shed_dx - sin_y * shed_dy
        wy = b.y_m + sin_y * shed_dx + cos_y * shed_dy
        if abs(wx) > half or abs(wy) > half:
            continue
        z = _z_at(heightmap_m, region.side_m, wx, wy)
        # We use the gate material as a tag for "small garden building"
        # in the renderer, since gate placeholder is a small box that
        # also reads believably as a shed. Width≈3 m, height≈2.4 m.
        shed_yaw = b.yaw_rad + rng.uniform(-0.2, 0.2)
        hedges.append(HedgeSegment(
            x=wx, y=wy, z=z, length_m=3.0,
            width_m=2.2, height_m=2.4, yaw=shed_yaw, material="gate",
        ))


def place_foliage(osm: OSMData, region: Region, heightmap_m: np.ndarray, *,
                  seed: int = 7, class_map: np.ndarray | None = None) -> FoliageResult:
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
                            P.contains(Point(x + ox, y + oy)))
        for x, y in pts:
            if len(trees) >= MAX_TREES:
                break
            wx, wy = x + cx_local, y + cy_local
            sub_seed = int(abs(hash((round(wx, 1), round(wy, 1))))) & 0xFFFFFFFF
            sr = np.random.default_rng(sub_seed)
            height = float(6.0 + sr.random() * 9.0)
            radius = float(2.0 + sr.random() * 1.5)
            yaw = float(sr.random() * 2 * np.pi)
            z = _z_at(heightmap_m, region.side_m, wx, wy)
            # Forest trees prefer mature plantation species (sitka spruce
            # for commercial woodlands, oak for native hardwood), falling
            # back to whatever the library has.
            lib_pick = pick_tree(sub_seed, prefer=("sitka_spruce", "oak"))
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
    # Skipped in v1: Overpass response strips tags from `nodes` so we'd
    # need the raw cache to look up `natural=tree` per node. Forest
    # polygons already give us 4000+ trees so this rarely matters.
    standalone = 0

    # ---- 3) Hedgerows ----
    hedges: list[HedgeSegment] = []
    # Road carriageway union — used for "don't put hedges/trees here"
    # checks throughout the rest of this function. Always declared at
    # function scope so later blocks can reference it even when the
    # synth-hedge stage early-exits (e.g. on a large map where OSM-
    # tagged hedges already saturate MAX_HEDGE_SEGMENTS).
    no_hedge_zone = None

    def _emit_segment(cx, cy, seg_len, yaw, *, width_m, height_m, material="hedge"):
        if abs(cx) > half or abs(cy) > half:
            return
        # Hedge bank — real NI hedges grow on a 20-40 cm earth mound. Lift
        # walls only slightly (5 cm — drystone walls aren't usually banked
        # the same way) so they still sit on the ground.
        z = _z_at(heightmap_m, region.side_m, cx, cy)
        if material == "wall":
            z += 0.05
        else:
            z += 0.25
        hedges.append(HedgeSegment(
            x=cx, y=cy, z=z, length_m=seg_len,
            width_m=width_m, height_m=height_m, yaw=yaw, material=material,
        ))

    def _emit_along_line(line: LineString, *, width_m: float, height_m: float,
                         material: str = "hedge"):
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
                          width_m=width_m, height_m=height_m, material=material)

    # 3a) Tagged OSM hedges / walls / fences — primary source. Three
    # materials:
    #   * `barrier=wall|stone_wall|drystone_wall|city_wall` → wall (grey)
    #   * `barrier=fence|wire_fence|chain_link|guard_rail` → fence (timber)
    #   * `barrier=hedge|hedgerow` or `natural=tree_row` → hedge (leafy)
    _WALL_BARRIERS  = {"wall", "stone_wall", "drystone_wall", "city_wall"}
    _FENCE_BARRIERS = {"fence", "wire_fence", "chain_link", "guard_rail"}
    for w in osm.ways:
        if len(hedges) >= MAX_HEDGE_SEGMENTS:
            break
        tags = w.get("tags") or {}
        barrier = tags.get("barrier")
        is_hedge = barrier in ("hedge", "hedgerow") \
            or barrier in _WALL_BARRIERS \
            or barrier in _FENCE_BARRIERS \
            or tags.get("natural") in ("tree_row",)
        if not is_hedge:
            continue
        line_ll = way_line_ll(w, osm.nodes)
        line = _project_line(line_ll, cx_world, cy_world)
        if line is None or line.length < 1.0:
            continue
        if barrier in _WALL_BARRIERS:
            _emit_along_line(line, width_m=0.5, height_m=1.1, material="wall")
        elif barrier in _FENCE_BARRIERS:
            _emit_along_line(line, width_m=0.15, height_m=1.1, material="fence")
        else:
            _emit_along_line(line, width_m=0.9, height_m=1.6, material="hedge")

    # Pre-compute the union of all road CARRIAGEWAYS as a no-hedge zone.
    # Used by the synth-hedge, field-boundary, farm-gate, and tree-prune
    # passes. Computed unconditionally (not gated on hedge cap) so big
    # maps that saturate MAX_HEDGE_SEGMENTS during 3a still get their
    # tree pruning + gate detection driven by a real road map.
    carriageway_polys = []
    for w2 in osm.ways:
        tags2 = w2.get("tags") or {}
        hwy2 = tags2.get("highway")
        cfg2 = _ROAD_CLASSES_WITH_HEDGES.get(hwy2)
        if cfg2 is None:
            continue
        line_ll2 = way_line_ll(w2, osm.nodes)
        line2 = _project_line(line_ll2, cx_world, cy_world)
        if line2 is None or line2.length < 1.0:
            continue
        try:
            carriageway_polys.append(line2.buffer(cfg2["width"] / 2.0 * 0.95))
        except Exception:
            continue
    if carriageway_polys:
        try:
            no_hedge_zone = unary_union(carriageway_polys)
        except Exception:
            no_hedge_zone = None

    # 3b) Synthesised hedges along rural road centrelines. NI roads almost
    # always have hedges either side but they're rarely tagged. We offset
    # the road centreline by half-width + a small gap, on both sides.
    if len(hedges) < MAX_HEDGE_SEGMENTS:

        def _emit_along_line_clipped(g, *, width_m, height_m):
            """Emit hedge segments along g, skipping any segment whose midpoint
            falls inside the no-hedge zone (road carriageway union)."""
            if g.length < 3.0:
                return
            n_seg = max(1, int(np.ceil(g.length / HEDGE_SEGMENT_LEN_M)))
            for i in range(n_seg):
                if len(hedges) >= MAX_HEDGE_SEGMENTS:
                    return
                t0, t1 = i / n_seg, (i + 1) / n_seg
                p0 = g.interpolate(t0, normalized=True)
                p1 = g.interpolate(t1, normalized=True)
                cx, cy = (p0.x + p1.x) / 2, (p0.y + p1.y) / 2
                # Drop segments inside the road network (junctions, lay-bys)
                if no_hedge_zone is not None and no_hedge_zone.contains(Point(cx, cy)):
                    continue
                if abs(cx) > half or abs(cy) > half:
                    continue
                seg_len = float(p0.distance(p1))
                yaw = float(np.arctan2(p1.y - p0.y, p1.x - p0.x))
                z = _z_at(heightmap_m, region.side_m, cx, cy)
                hedges.append(HedgeSegment(
                    x=cx, y=cy, z=z, length_m=seg_len,
                    width_m=width_m, height_m=height_m, yaw=yaw,
                ))

        for w in osm.ways:
            if len(hedges) >= MAX_HEDGE_SEGMENTS:
                break
            tags = w.get("tags") or {}
            hwy = tags.get("highway")
            cfg = _ROAD_CLASSES_WITH_HEDGES.get(hwy)
            if cfg is None:
                continue
            line_ll = way_line_ll(w, osm.nodes)
            line = _project_line(line_ll, cx_world, cy_world)
            if line is None or line.length < 5.0:
                continue
            offset_dist = cfg["width"] / 2.0 + cfg["hedge_offset"]
            try:
                left = line.parallel_offset(offset_dist, "left", join_style=1)
                right = line.parallel_offset(offset_dist, "right", join_style=1)
            except Exception:
                continue
            for off in (left, right):
                if off is None or off.is_empty:
                    continue
                geoms = [off] if hasattr(off, "coords") else list(getattr(off, "geoms", []))
                for g in geoms:
                    try:
                        _emit_along_line_clipped(g, width_m=0.9, height_m=1.4)
                    except Exception:
                        continue

    # 3c) FIELD-BOUNDARY hedges. Trace the perimeter of every farmland /
    # meadow / pasture polygon and emit hedge segments along it. This is
    # what produces the iconic NI patchwork — solid green hedges
    # separating coloured field tiles. We dissolve coincident shared
    # boundaries via unary_union so neighbouring fields share one hedge.
    # ~25% of synthesised segments randomise to drystone walls so the
    # whole landscape isn't uniform leafy hedge.
    if len(hedges) < MAX_HEDGE_SEGMENTS:
        # Two pools: rough/upland (pasture, grassland) gets a high wall
        # ratio; improved farmland (farmland, meadow, grass, park) leans
        # heavily toward leafy hedge. Real NI fits this split closely.
        rough_polys: list[Polygon] = []      # → 50% walls
        improved_polys: list[Polygon] = []   # → 20% walls
        for w in osm.ways:
            tags = w.get("tags") or {}
            lu = tags.get("landuse")
            nat = tags.get("natural")
            ls = tags.get("leisure")
            if lu == "pasture" or nat == "grassland":
                bucket = rough_polys
            elif lu in ("farmland", "meadow", "grass") or ls == "park":
                bucket = improved_polys
            else:
                continue
            ring = way_polygon_ll(w, osm.nodes)
            if ring is None:
                continue
            poly = _project_polygon(ring, cx_world, cy_world)
            if poly is None or poly.area < 200.0:
                continue
            bucket.append(poly)

        road_keepout = None
        try:
            road_keepout = (no_hedge_zone.buffer(1.5)
                            if no_hedge_zone is not None else None)
        except Exception:
            road_keepout = None

        def _emit_field_boundaries(polys: list[Polygon], wall_prob: float, seed_offset: int):
            if not polys or len(hedges) >= MAX_HEDGE_SEGMENTS:
                return
            try:
                merged = unary_union(polys)
            except Exception:
                return
            bnd = merged.boundary
            if isinstance(bnd, LineString):
                boundary_lines = [bnd]
            elif isinstance(bnd, MultiLineString):
                boundary_lines = list(bnd.geoms)
            else:
                boundary_lines = []
            wall_rng = np.random.default_rng(0xBADF1E1D + seed_offset)
            for bl in boundary_lines:
                if len(hedges) >= MAX_HEDGE_SEGMENTS:
                    break
                if bl.is_empty or bl.length < 4.0:
                    continue
                g = bl
                if road_keepout is not None:
                    try:
                        g = bl.difference(road_keepout)
                    except Exception:
                        g = bl
                if g.is_empty:
                    continue
                parts = [g] if isinstance(g, LineString) else list(getattr(g, "geoms", []))
                for part in parts:
                    if not isinstance(part, LineString) or part.length < 4.0:
                        continue
                    n_seg = max(1, int(np.ceil(part.length / HEDGE_SEGMENT_LEN_M)))
                    for i in range(n_seg):
                        if len(hedges) >= MAX_HEDGE_SEGMENTS:
                            break
                        t0, t1 = i / n_seg, (i + 1) / n_seg
                        p0 = part.interpolate(t0, normalized=True)
                        p1 = part.interpolate(t1, normalized=True)
                        cx, cy = (p0.x + p1.x) / 2, (p0.y + p1.y) / 2
                        if abs(cx) > half or abs(cy) > half:
                            continue
                        seg_len = float(p0.distance(p1))
                        yaw = float(np.arctan2(p1.y - p0.y, p1.x - p0.x))
                        mat = "wall" if wall_rng.random() < wall_prob else "hedge"
                        # Field-boundary hedges are taller and slightly
                        # wider than road-side hedges so they're visible
                        # from any altitude above the playable area.
                        _emit_segment(cx, cy, seg_len, yaw,
                                      width_m=(0.45 if mat == "wall" else 1.0),
                                      height_m=(1.0 if mat == "wall" else 2.0),
                                      material=mat)

        _emit_field_boundaries(rough_polys,    wall_prob=0.50, seed_offset=0)
        _emit_field_boundaries(improved_polys, wall_prob=0.20, seed_offset=1)
        boundary_count_so_far = len(hedges)

    # 3c.fallback) When OSM had few or no farmland polygons, synthesise a
    # jittered grid of hedges across grass/pasture areas of the class map
    # so the map doesn't read as bare. Only runs when both the class map
    # is available AND we got fewer than ~80 hedges from the OSM
    # boundary code — otherwise the OSM coverage is fine on its own.
    if class_map is not None and len(hedges) < MAX_HEDGE_SEGMENTS:
        # Heuristic: count tagged-barrier + boundary hedges and decide
        # whether we need synthetic ones. A 2 km map should have hundreds
        # of hedges; under 200 means we're missing field boundaries.
        if len(hedges) < 200:
            cmap_h, cmap_w = class_map.shape
            GRASS_CLASSES = {2, 3}     # lawn + pasture (skip forest = 7)
            cell_m = 80.0              # ~80 m fields
            jit_amp = cell_m * 0.18    # cell-size jitter
            grid_rng = np.random.default_rng(seed + 4242)
            n_cells = max(2, int(round(region.side_m / cell_m)))
            road_keepout_grid = None
            try:
                if no_hedge_zone is not None:
                    road_keepout_grid = no_hedge_zone.buffer(2.5)
            except Exception:
                road_keepout_grid = None

            def _is_grass(x_m: float, y_m: float) -> bool:
                u = (x_m + half) / region.side_m
                v = 1.0 - (y_m + half) / region.side_m
                col = int(np.clip(round(u * (cmap_w - 1)), 0, cmap_w - 1))
                row = int(np.clip(round(v * (cmap_h - 1)), 0, cmap_h - 1))
                return int(class_map[row, col]) in GRASS_CLASSES

            def _emit_grid_segments_along(direction: str):
                if direction == "x":
                    for j in range(1, n_cells):
                        if len(hedges) >= MAX_HEDGE_SEGMENTS:
                            return
                        y_line = -half + j * cell_m + grid_rng.uniform(-jit_amp, jit_amp)
                        x = -half
                        while x < half - HEDGE_SEGMENT_LEN_M:
                            x_end = min(x + HEDGE_SEGMENT_LEN_M, half)
                            cx = (x + x_end) / 2
                            cy = y_line + grid_rng.uniform(-jit_amp * 0.3, jit_amp * 0.3)
                            x = x_end
                            if not _is_grass(cx, cy):
                                continue
                            if road_keepout_grid is not None and \
                                    road_keepout_grid.contains(Point(cx, cy)):
                                continue
                            seg_len = x_end - (x_end - HEDGE_SEGMENT_LEN_M)
                            wall = grid_rng.random() < 0.30
                            _emit_segment(cx, cy, HEDGE_SEGMENT_LEN_M, 0.0,
                                          width_m=(0.45 if wall else 1.0),
                                          height_m=(1.0 if wall else 2.0),
                                          material=("wall" if wall else "hedge"))
                else:  # y direction
                    for i in range(1, n_cells):
                        if len(hedges) >= MAX_HEDGE_SEGMENTS:
                            return
                        x_line = -half + i * cell_m + grid_rng.uniform(-jit_amp, jit_amp)
                        y = -half
                        while y < half - HEDGE_SEGMENT_LEN_M:
                            y_end = min(y + HEDGE_SEGMENT_LEN_M, half)
                            cy = (y + y_end) / 2
                            cx = x_line + grid_rng.uniform(-jit_amp * 0.3, jit_amp * 0.3)
                            y = y_end
                            if not _is_grass(cx, cy):
                                continue
                            if road_keepout_grid is not None and \
                                    road_keepout_grid.contains(Point(cx, cy)):
                                continue
                            wall = grid_rng.random() < 0.30
                            _emit_segment(cx, cy, HEDGE_SEGMENT_LEN_M, np.pi / 2,
                                          width_m=(0.45 if wall else 1.0),
                                          height_m=(1.0 if wall else 2.0),
                                          material=("wall" if wall else "hedge"))

            _emit_grid_segments_along("x")
            _emit_grid_segments_along("y")

    # ---- 3d) FARM GATES at hedge-road crossings ----
    # Where a hedge segment runs close to a road carriageway, we replace
    # one hedge segment in that area with a wooden gate. Real fields
    # need access points; without these every field looks sealed off.
    if no_hedge_zone is not None and hedges:
        try:
            # Buffer the no_hedge_zone by 4m so segments AT the road edge
            # qualify (the zone is the road itself, gates sit beside it)
            gate_band = no_hedge_zone.buffer(5.0).difference(no_hedge_zone)
            # Stride through hedges and look for ones whose midpoint is in
            # the band — flag every Nth as a gate so we don't get a
            # continuous run of gates around a single junction.
            gate_rng = np.random.default_rng(seed + 42)
            for i, h in enumerate(hedges):
                if h.material != "hedge":
                    continue
                if not gate_band.contains(Point(h.x, h.y)):
                    continue
                if gate_rng.random() > 0.18:    # ~18% of qualifying segments
                    continue
                # Replace the segment with a gate variant. Same yaw/centre
                # but narrower height & timber colour.
                hedges[i] = HedgeSegment(
                    x=h.x, y=h.y, z=h.z,
                    length_m=4.0,        # standard farm gate ~4m wide
                    width_m=0.12,
                    height_m=1.3,
                    yaw=h.yaw,
                    material="gate",
                )
        except Exception:
            pass

    # ---- 4) HEDGEROW TREES ----
    # Real NI hedges have small mature trees (hawthorn, ash, sycamore,
    # occasional oak) sprinkled along them every 25-40 m. Adds a huge
    # amount of "lived-in farmland" feel — without these the hedges read
    # as continuous green slabs from any altitude. Cap count so we never
    # explode tree totals: max 25% of MAX_TREES gets spent on hedge trees.
    if hedges and len(trees) < MAX_TREES:
        hedge_tree_budget = max(0, min(MAX_TREES // 4, MAX_TREES - len(trees)))
        # Group hedge segments into continuous runs so trees space evenly
        # within each run rather than appearing per-segment. We approximate
        # this by sampling the segment list every ~28 m of cumulative
        # length, biased to longer hedges getting more trees.
        cumulative = 0.0
        next_tree_at = rng.uniform(8.0, 28.0)   # offset so first tree isn't at 0
        ht_rng = np.random.default_rng(seed + 999)
        for h in hedges:
            if h.material == "wall":
                continue   # walls don't get trees on top
            cumulative += h.length_m
            if cumulative < next_tree_at:
                continue
            if hedge_tree_budget <= 0:
                break
            # Place tree at the segment midpoint with a small lateral
            # offset so it doesn't merge into the hedge centreline.
            wx = h.x + np.cos(h.yaw + np.pi / 2) * ht_rng.uniform(-0.4, 0.4)
            wy = h.y + np.sin(h.yaw + np.pi / 2) * ht_rng.uniform(-0.4, 0.4)
            if abs(wx) > half or abs(wy) > half:
                next_tree_at = cumulative + ht_rng.uniform(20.0, 36.0)
                continue
            sub_seed = int(abs(hash((round(wx, 1), round(wy, 1))))) & 0xFFFFFFFF
            sr = np.random.default_rng(sub_seed)
            # Hedge trees are smaller + more variable than forest trees:
            # young ash 4-8m, occasional mature ash/oak 10-14m
            mature = sr.random() < 0.18
            height = float((10.0 + sr.random() * 4.0) if mature
                           else (4.0 + sr.random() * 4.0))
            radius = float((1.6 + sr.random() * 0.8) if mature
                           else (0.8 + sr.random() * 0.7))
            yaw = float(sr.random() * 2 * np.pi)
            z = _z_at(heightmap_m, region.side_m, wx, wy)
            # Hedgerow trees prefer hawthorn (the dominant NI hedge tree),
            # then oak for the rare mature specimens.
            lib_pick = pick_tree(sub_seed, prefer=("hawthorn", "oak"))
            shape_rel = lib_pick.rel_path if lib_pick else "art/shapes/foliage/tree.dae"
            species = lib_pick.type_label if lib_pick else "default"
            trees.append(TreePlacement(
                x=wx, y=wy, z=z,
                scale_xyz=(radius, radius, height),
                yaw=yaw,
                shape_relpath=shape_rel, species=species,
            ))
            hedge_tree_budget -= 1
            # 25-40 m to next tree along the cumulative run
            next_tree_at = cumulative + ht_rng.uniform(22.0, 40.0)

    # ---- 5) FINAL TIDY-UP ----
    # Drop any tree whose XY falls inside the road carriageway buffer —
    # forest polygons sometimes overlap with roads (because OSM's
    # `landuse=forest` polygon can contain the road centreline). Same
    # check post-hoc for hedges that might have leaked through.
    if no_hedge_zone is not None:
        try:
            keepout = no_hedge_zone.buffer(0.5)   # tiny pad
            trees = [t for t in trees if not keepout.contains(Point(t.x, t.y))]
            hedges = [h for h in hedges if not keepout.contains(Point(h.x, h.y))]
        except Exception:
            pass

    # ---- 6) BUSHES — rural-only, road-side + scattered in fields ----
    bushes = _place_bushes(
        osm=osm, region=region, heightmap_m=heightmap_m,
        cx_world=cx_world, cy_world=cy_world, half=half, seed=seed,
        no_hedge_zone=no_hedge_zone, class_map=class_map,
    )
    trees.extend(bushes)

    return FoliageResult(
        trees=trees,
        hedges=hedges,
        forest_polys=len(forest_polys),
        standalone_trees=standalone,
    )


# ---------------------------------------------------------------------------
# Bush placement — small low-poly foliage scattered in rural fields and
# along rural road edges. Uses the existing TreePlacement carrier with
# species="bush" so the export loop ships them as TSStatics referencing
# `art/shapes/foliage/bush.dae` (an 8-tri octahedron — cheap enough for
# hundreds of instances).
# ---------------------------------------------------------------------------

# Tunables (kept low for performance — 8-tri bushes scale up to ~600 OK)
_BUSH_FIELD_SPACING_M = 18.0   # Poisson r — denser scatter in fields (was 28)
_BUSH_ROAD_SPACING_M  = 7.0    # along rural road edges (was 14)
_BUSH_ROAD_OFFSET_M   = 1.8    # perpendicular distance from road centreline
_BUSH_ROAD_GAP_DRIVEWAY_M = 5.0  # leave gap where driveways meet roads
_BUSH_MAX_TOTAL = 2000          # tripled (was 600)

# Highways that get road-side bushes (rural classes only)
_RURAL_ROAD_CLASSES = {
    "primary", "secondary", "tertiary", "unclassified",
    "residential", "living_street", "lane", "track", "path",
    # NOTE: 'service' (driveways) deliberately excluded
}

# OSM landuse tags that mark URBAN areas where we skip bushes entirely
_URBAN_LANDUSE = {
    "residential", "commercial", "industrial", "retail",
    "education", "religious", "military", "construction",
}
_URBAN_NATURAL = set()  # nothing — natural=* tags are rural


def _build_urban_mask(osm: OSMData, cx_world: float, cy_world: float,
                     half: float) -> Polygon | None:
    """Union of OSM urban-landuse polygons, projected into terrain space."""
    polys: list[Polygon] = []
    for w in osm.ways:
        tags = w.get("tags") or {}
        lu = tags.get("landuse")
        if lu not in _URBAN_LANDUSE:
            continue
        ring = way_polygon_ll(w, osm.nodes)
        if ring is None:
            continue
        poly = _project_polygon(ring, cx_world, cy_world)
        if poly is None or poly.is_empty:
            continue
        polys.append(poly)
    if not polys:
        return None
    try:
        return unary_union(polys)
    except Exception:
        return None


def _build_rural_road_lines(osm: OSMData, cx_world: float, cy_world: float,
                            half: float) -> list[LineString]:
    """OSM rural-road centrelines projected into terrain space."""
    lines: list[LineString] = []
    for w in osm.ways:
        tags = w.get("tags") or {}
        if tags.get("highway") not in _RURAL_ROAD_CLASSES:
            continue
        line_ll = way_line_ll(w, osm.nodes)
        if line_ll is None:
            continue
        line = _project_line(line_ll, cx_world, cy_world)
        if line is None or line.is_empty:
            continue
        lines.append(line)
    return lines


def _build_driveway_endpoints(osm: OSMData, cx_world: float, cy_world: float,
                              half: float) -> list[Point]:
    """Project OSM service road endpoints — used to gap road-side bushes."""
    pts: list[Point] = []
    for w in osm.ways:
        tags = w.get("tags") or {}
        if tags.get("highway") != "service":
            continue
        line_ll = way_line_ll(w, osm.nodes)
        if line_ll is None:
            continue
        line = _project_line(line_ll, cx_world, cy_world)
        if line is None or line.is_empty:
            continue
        coords = list(line.coords)
        if coords:
            pts.append(Point(coords[0]))
            pts.append(Point(coords[-1]))
    return pts


def _place_bushes(*, osm: OSMData, region: Region, heightmap_m: np.ndarray,
                 cx_world: float, cy_world: float, half: float, seed: int,
                 no_hedge_zone, class_map: np.ndarray | None
                 ) -> list[TreePlacement]:
    rng = np.random.default_rng(seed ^ 0xB05BE5)
    urban = _build_urban_mask(osm, cx_world, cy_world, half)
    rural_roads = _build_rural_road_lines(osm, cx_world, cy_world, half)
    drive_pts = _build_driveway_endpoints(osm, cx_world, cy_world, half)
    drive_buffer = unary_union([p.buffer(_BUSH_ROAD_GAP_DRIVEWAY_M)
                                for p in drive_pts]) if drive_pts else None
    road_keepout = no_hedge_zone.buffer(0.0) if no_hedge_zone is not None else None

    # Class-map lookup helpers (skip non-rural surfaces like asphalt/water)
    cm_size = class_map.shape[0] if class_map is not None else 0
    BUSH_OK_CLASSES = {2, 3, 4, 7}  # lawn, pasture, earth, forest

    def _is_grassy(x: float, y: float) -> bool:
        if class_map is None:
            return True
        cx_idx = int((x + half) / region.side_m * cm_size)
        cy_idx = int((y + half) / region.side_m * cm_size)
        if not (0 <= cx_idx < cm_size and 0 <= cy_idx < cm_size):
            return False
        # class_map is row 0 = north, our world has +y = north
        return int(class_map[cm_size - 1 - cy_idx, cx_idx]) in BUSH_OK_CLASSES

    bushes: list[TreePlacement] = []

    # ---------- A) Road-side bushes (rural roads only) ----------
    for line in rural_roads:
        L = line.length
        if L < _BUSH_ROAD_SPACING_M:
            continue
        steps = int(L / _BUSH_ROAD_SPACING_M)
        for k in range(1, steps):
            if len(bushes) >= _BUSH_MAX_TOTAL:
                break
            t = k * _BUSH_ROAD_SPACING_M / L
            t = min(0.999, max(0.001, t + rng.uniform(-0.04, 0.04)))
            pt = line.interpolate(t * L)
            # Skip in urban areas
            if urban is not None and urban.contains(pt):
                continue
            # Skip near driveway endpoints (where service roads meet)
            if drive_buffer is not None and drive_buffer.contains(pt):
                continue
            # Tangent direction → perpendicular for offset
            t2 = min(0.999, t + 0.001)
            ahead = line.interpolate(t2 * L)
            dx = ahead.x - pt.x; dy = ahead.y - pt.y
            ln = (dx * dx + dy * dy) ** 0.5 or 1.0
            ux, uy = dx / ln, dy / ln
            nx, ny = -uy, ux
            # Place on alternating sides
            side = 1 if (k % 2 == 0) else -1
            off = _BUSH_ROAD_OFFSET_M + rng.uniform(0.0, 0.6)
            bx = pt.x + nx * off * side
            by = pt.y + ny * off * side
            if abs(bx) > half or abs(by) > half:
                continue
            # Don't sit ON the road carriageway
            if road_keepout is not None and road_keepout.contains(Point(bx, by)):
                continue
            if not _is_grassy(bx, by):
                continue
            sr = np.random.default_rng(int(abs(hash((round(bx, 1), round(by, 1))))) & 0xFFFFFFFF)
            scale = float(0.7 + sr.random() * 0.7)   # 0.7-1.4m bush
            yaw = float(sr.random() * 2 * np.pi)
            z = _z_at(heightmap_m, region.side_m, bx, by)
            bushes.append(TreePlacement(
                x=bx, y=by, z=z,
                scale_xyz=(scale, scale, scale * 0.7),
                yaw=yaw,
                shape_relpath="art/shapes/foliage/bush.dae",
                species="bush",
            ))
        if len(bushes) >= _BUSH_MAX_TOTAL:
            break

    # ---------- B) Field-scattered bushes ----------
    field_budget = max(0, _BUSH_MAX_TOTAL - len(bushes))
    if field_budget > 0:
        # Poisson-disk over the terrain bounding box
        def _mask_field(x: float, y: float) -> bool:
            if abs(x) > half - 5 or abs(y) > half - 5:
                return False
            if urban is not None and urban.contains(Point(x, y)):
                return False
            if road_keepout is not None and road_keepout.contains(Point(x, y)):
                return False
            return _is_grassy(x, y)
        pts = _poisson_disk(2 * half, 2 * half,
                           _BUSH_FIELD_SPACING_M, seed=seed + 17,
                           mask=_mask_field)
        for x, y in pts:
            if len(bushes) >= _BUSH_MAX_TOTAL:
                break
            sr = np.random.default_rng(int(abs(hash((round(x, 1), round(y, 1))))) & 0xFFFFFFFF)
            scale = float(0.6 + sr.random() * 0.9)   # 0.6-1.5m
            yaw = float(sr.random() * 2 * np.pi)
            z = _z_at(heightmap_m, region.side_m, x, y)
            bushes.append(TreePlacement(
                x=x, y=y, z=z,
                scale_xyz=(scale, scale, scale * 0.7),
                yaw=yaw,
                shape_relpath="art/shapes/foliage/bush.dae",
                species="bush",
            ))

    return bushes

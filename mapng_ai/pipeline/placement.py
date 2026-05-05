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
from shapely.geometry import LineString, Point, Polygon

from mapng_ai.assets.base import AssetProvider, BuildingAsset
from mapng_ai.pipeline.region import Region, sample_terrain_height as _z_at
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


# ---------------------------------------------------------------------------
# Settlement classifier — picks a "context tag" per building that
# influences which placeholder type (or which Italy whitelist subset)
# the placement uses.
#
# Context tags (in priority order):
#   industrial    — inside OSM landuse=industrial polygon
#   commercial    — inside OSM landuse=commercial/retail polygon
#   town_centre   — high local building density (>=8 nearby)
#   suburb        — medium local density (3-7) or inside landuse=residential
#   village       — small isolated cluster (place=village/hamlet within 200m)
#   rural         — everything else (default — isolated farms, barns, cottages)
# ---------------------------------------------------------------------------

def _build_zone_polygons(osm: OSMData, cx_world: float, cy_world: float,
                         landuse_values: set[str]) -> Polygon | None:
    """Project + union OSM landuse polygons matching the given values."""
    polys: list[Polygon] = []
    for w in osm.ways:
        tags = w.get("tags") or {}
        if tags.get("landuse") not in landuse_values:
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
            if poly.is_valid and not poly.is_empty:
                polys.append(poly)
        except Exception:
            continue
    if not polys:
        return None
    try:
        from shapely.ops import unary_union
        return unary_union(polys)
    except Exception:
        return None


def _building_centres(osm: OSMData, cx_world: float, cy_world: float
                     ) -> list[tuple[float, float]]:
    """Return all OSM building centroids in terrain-local coords."""
    out: list[tuple[float, float]] = []
    for w in osm.ways:
        tags = w.get("tags") or {}
        if "building" not in tags:
            continue
        ring_ll = way_polygon_ll(w, osm.nodes)
        if ring_ll is None:
            continue
        try:
            lon, lat = zip(*ring_ll)
            xs, ys = _LL_TO_ITM.transform(list(lon), list(lat))
            cx = sum(xs) / len(xs) - cx_world
            cy = sum(ys) / len(ys) - cy_world
            out.append((cx, cy))
        except Exception:
            continue
    return out


def _classify_settlement(cx: float, cy: float,
                         industrial_zone, commercial_zone, residential_zone,
                         all_centres: list[tuple[float, float]]) -> str:
    """Classify a building's local settlement context. Returns one of:
    industrial, commercial, town_centre, suburb, village, rural."""
    pt = Point(cx, cy)
    if industrial_zone is not None and industrial_zone.contains(pt):
        return "industrial"
    if commercial_zone is not None and commercial_zone.contains(pt):
        return "commercial"
    # Local density: count buildings within 100 m
    density = sum(1 for (bx, by) in all_centres
                  if (bx - cx) ** 2 + (by - cy) ** 2 < 100 * 100)
    if density >= 8:
        return "town_centre"
    if density >= 3:
        return "suburb"
    if residential_zone is not None and residential_zone.contains(pt):
        return "village"
    return "rural"


# Settlement → preferred placeholder type override.
# Maps OSM `building=*` → final placeholder type, given the surrounding
# context. e.g. residential + town_centre → 3-storey apartment;
# residential + rural → smaller "house".
_SETTLEMENT_TYPE_REMAP: dict[tuple[str, str], str] = {
    # (osm_type, settlement) → placeholder type
    ("residential", "town_centre"): "apartment",
    ("residential", "suburb"):       "semi",
    ("residential", "village"):      "house",
    ("residential", "rural"):        "house",
    ("default",     "town_centre"): "apartment",
    ("default",     "suburb"):       "residential",
    ("default",     "village"):      "house",
    ("default",     "rural"):        "house",
    ("default",     "industrial"):   "warehouse",
    ("default",     "commercial"):   "shop",
    ("yes",         "industrial"):   "warehouse",
    ("yes",         "commercial"):   "shop",
    ("house",       "rural"):        "house",
    ("house",       "village"):      "house",
}


def _remap_type(osm_type: str, settlement: str) -> str:
    """Pick the placeholder type given OSM tag + settlement context."""
    key = (osm_type, settlement)
    if key in _SETTLEMENT_TYPE_REMAP:
        return _SETTLEMENT_TYPE_REMAP[key]
    # No specific override — keep the OSM type
    return osm_type


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


# `_z_at` is the canonical height sampler from region.py (imported above).


# OSM highway types we use for "nearest road alignment"
_ROAD_HIGHWAYS = {
    "primary", "secondary", "tertiary", "unclassified",
    "residential", "living_street", "service", "lane", "track",
}

# Buildings closer than this metres to a road get their long axis
# aligned to the road tangent. Beyond this they keep their OSM yaw.
_ROAD_ALIGN_RADIUS_M = 40.0


def _build_road_lines(osm, cx_world: float, cy_world: float) -> list[LineString]:
    """Project OSM road centrelines into terrain-local coords.
    Used to find each building's nearest road for orientation alignment."""
    from mapng_ai.sources.overpass import way_line_ll as _way_line_ll
    lines: list[LineString] = []
    for w in osm.ways:
        tags = w.get("tags") or {}
        if tags.get("highway") not in _ROAD_HIGHWAYS:
            continue
        line_ll = _way_line_ll(w, osm.nodes)
        if not line_ll or len(line_ll) < 2:
            continue
        try:
            lon, lat = zip(*line_ll)
            xs, ys = _LL_TO_ITM.transform(list(lon), list(lat))
            xs_local = [x - cx_world for x in xs]
            ys_local = [y - cy_world for y in ys]
            ls = LineString(zip(xs_local, ys_local))
            if not ls.is_empty and ls.length > 1.0:
                lines.append(ls)
        except Exception:
            continue
    return lines


def _nearest_road_yaw(pt: Point, roads: list[LineString],
                     fallback_yaw: float, max_dist_m: float = _ROAD_ALIGN_RADIUS_M) -> float:
    """Find the nearest road segment within max_dist; return the yaw of
    its tangent direction at the closest point. Falls back to OSM yaw
    if no road is close enough.

    The returned yaw aligns a building's LONG axis along the road —
    which is what we want for European villages where houses line the
    road, parallel to it. We also flip 180° if needed so the building
    "faces" the same general direction as adjacent buildings would.
    """
    if not roads:
        return fallback_yaw
    nearest = None
    min_d = float("inf")
    for road in roads:
        d = road.distance(pt)
        if d < min_d:
            min_d = d
            nearest = road
    if nearest is None or min_d > max_dist_m:
        return fallback_yaw
    try:
        # Sample tangent: project, then take points slightly before/after
        proj = nearest.project(pt)
        L = nearest.length
        d1 = max(0.0, proj - 0.5)
        d2 = min(L, proj + 0.5)
        if d2 - d1 < 1e-6:
            return fallback_yaw
        p0 = nearest.interpolate(d1)
        p1 = nearest.interpolate(d2)
        return float(np.arctan2(p1.y - p0.y, p1.x - p0.x))
    except Exception:
        return fallback_yaw


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
    placed_polys: list[Polygon] = []   # for overlap dedupe
    road_lines = _build_road_lines(osm, cx_world, cy_world)

    # Pre-compute settlement zones (one-time cost)
    industrial_zone = _build_zone_polygons(osm, cx_world, cy_world,
                                           {"industrial", "construction"})
    commercial_zone = _build_zone_polygons(osm, cx_world, cy_world,
                                           {"commercial", "retail"})
    residential_zone = _build_zone_polygons(osm, cx_world, cy_world,
                                            {"residential"})
    all_centres = _building_centres(osm, cx_world, cy_world)

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

        # Skip buildings that significantly overlap one we already
        # placed — OSM often has overlapping polygons (main + extension,
        # or duplicate digitisations) that produce stacked buildings.
        try:
            skip = False
            for prev in placed_polys:
                if not poly.intersects(prev):
                    continue
                inter = poly.intersection(prev).area
                if inter > 0.5 * min(poly.area, prev.area):
                    skip = True
                    break
            if skip:
                continue
        except Exception:
            pass
        placed_polys.append(poly)

        btype = _infer_type(tags)
        # Apply settlement-context remap: e.g. residential + town_centre
        # becomes "apartment" so we get the multi-storey placeholder.
        settlement = _classify_settlement(cx_local, cy_local,
                                         industrial_zone, commercial_zone,
                                         residential_zone, all_centres)
        btype = _remap_type(btype, settlement)
        levels, height = _infer_levels_and_height(tags, poly.area)
        seed = int(way["id"])
        asset = provider.get_building(
            footprint_m2=float(poly.area),
            levels=levels,
            building_type=btype,
            seed=seed,
        )
        nat_l, nat_w, nat_h = asset.natural_size_m

        # Per-building deterministic jitter so neighbours don't read as
        # exact clones even when they share an asset reference. ±4%
        # scale on each axis + ±0.04 rad yaw nudge — enough to break
        # repetition, small enough to keep the building square to the
        # OBB long edge (which is the architecturally correct yaw).
        jitter = np.random.default_rng(seed & 0xFFFFFFFF)
        sj = float(jitter.uniform(0.96, 1.04))   # uniform across xy
        szj = float(jitter.uniform(0.97, 1.03))  # vertical
        yaw_jitter = float(jitter.uniform(-0.04, 0.04))

        # Per spec: scale uniformly, clamp to ±40 %
        target_scale = max(length / max(nat_l, 1e-3), width / max(nat_w, 1e-3))
        target_scale = max(0.6, min(1.4, target_scale))
        scale_xyz = (length * sj, width * sj, height * szj)

        # Re-orient so the building's long axis is parallel to the
        # nearest road. NI (and most European) villages have houses
        # lining the road parallel to it — this looks much cleaner than
        # blindly using whatever angle OSM has tagged. Buildings >40m
        # from any road keep their OSM yaw (rural barns/sheds at random
        # field angles).
        building_centre = Point(cx_local, cy_local)
        aligned_yaw = _nearest_road_yaw(building_centre, road_lines,
                                        fallback_yaw=yaw)
        # Pick whichever 90° rotation of `aligned_yaw` is closer to the
        # OSM yaw — that way "long-axis along road" is preserved but
        # we don't flip a building 90° when OSM had it perpendicular.
        candidates = [aligned_yaw, aligned_yaw + np.pi / 2]
        def _angle_diff(a, b):
            d = (a - b + np.pi) % (2 * np.pi) - np.pi
            return abs(d)
        yaw = min(candidates, key=lambda c: _angle_diff(c, yaw))

        cos_y, sin_y = np.cos(yaw), np.sin(yaw)
        hl, hw = length * 0.5, width * 0.5
        corners = [(hl, hw), (hl, -hw), (-hl, hw), (-hl, -hw)]
        z_samples = []
        for lx, ly in corners:
            wx = cx_local + cos_y * lx - sin_y * ly
            wy = cy_local + sin_y * lx + cos_y * ly
            z_samples.append(_z_at(heightmap_m, region.side_m, wx, wy))
        z = min(z_samples) - 0.3
        placements.append(BuildingPlacement(
            osm_id=seed, asset=asset,
            x_m=cx_local, y_m=cy_local, z_m=z,
            yaw_rad=yaw + yaw_jitter, scale_xyz=scale_xyz,
        ))
    return placements

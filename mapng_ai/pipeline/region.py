"""Stage 1 — region resolution.

Converts the user's lat/lon bbox into a working frame:
- working CRS = Irish Transverse Mercator (EPSG:2157)
- a square ITM bbox of side `target_size_m` centred on the user's request
"""
from __future__ import annotations

from dataclasses import dataclass

from pyproj import Transformer

from mapng_ai.sources.base import BBoxITM, BBoxLL


# Cached, since pyproj transformer setup isn't free
_TO_ITM = Transformer.from_crs("EPSG:4326", "EPSG:2157", always_xy=True)
_TO_LL = Transformer.from_crs("EPSG:2157", "EPSG:4326", always_xy=True)


@dataclass(frozen=True)
class Region:
    request_ll: BBoxLL          # what the user drew
    fetch_ll: BBoxLL            # slightly buffered lat/lon for source fetching
    working_itm: BBoxITM        # square ITM bbox (the BeamNG terrain extent)
    side_m: float               # square side length in metres
    heightmap_size: int         # output texel count per side


def resolve_region(bbox: BBoxLL, target_size_m: float | None = None,
                   heightmap_size: int = 2048,
                   *, min_side_m: float = 500.0, max_side_m: float = 8000.0) -> Region:
    """Convert a lat/lon bbox into the square ITM working area.

    Side length: by default we use the LARGER of the bbox's projected
    width or height in metres (clamped to [min_side_m, max_side_m]) so the
    user's drawn rectangle determines the level size. Pass an explicit
    `target_size_m` to override.
    """
    # 1) Reproject corners to ITM
    xs, ys = _TO_ITM.transform(
        [bbox.west, bbox.east], [bbox.south, bbox.north]
    )
    cx = (xs[0] + xs[1]) / 2
    cy = (ys[0] + ys[1]) / 2

    # 2) Decide side: explicit override OR the larger drawn dimension.
    if target_size_m is None:
        width_m  = abs(xs[1] - xs[0])
        height_m = abs(ys[1] - ys[0])
        target_size_m = max(width_m, height_m)
    target_size_m = max(min_side_m, min(max_side_m, target_size_m))

    half = target_size_m / 2
    working = BBoxITM(west=cx - half, south=cy - half, east=cx + half, north=cy + half)

    # 3) Reproject the square back to lat/lon, then add a small buffer for source fetch
    wx, ex = working.west, working.east
    sy, ny = working.south, working.north
    lons, lats = _TO_LL.transform([wx, ex, wx, ex], [sy, sy, ny, ny])
    fetch_w, fetch_e = min(lons), max(lons)
    fetch_s, fetch_n = min(lats), max(lats)
    buf = 0.002  # ~200 m at NI latitudes — gives the reproject some bleed room
    fetch = BBoxLL(
        west=fetch_w - buf, south=fetch_s - buf, east=fetch_e + buf, north=fetch_n + buf
    )

    return Region(
        request_ll=bbox,
        fetch_ll=fetch,
        working_itm=working,
        side_m=target_size_m,
        heightmap_size=heightmap_size,
    )


def itm_to_ll_bbox(b: BBoxITM) -> BBoxLL:
    lons, lats = _TO_LL.transform([b.west, b.east, b.west, b.east], [b.south, b.south, b.north, b.north])
    return BBoxLL(west=min(lons), south=min(lats), east=max(lons), north=max(lats))


def sample_terrain_height(heightmap_m, side_m: float, x_m: float, y_m: float) -> float:
    """Bilinear-ish sample of the terrain heightmap at world XY (origin at
    terrain centre). Lives here so every placement stage references one
    canonical implementation; previously copies in foliage/decal_roads/
    placement could drift out of sync.

    Heightmap is `(size, size)` numpy with row 0 = NORTH (image space).
    """
    size = heightmap_m.shape[0]
    half = side_m / 2
    # Clip then convert to fractional pixel
    u = max(0.0, min(1.0, (x_m + half) / side_m))
    v = max(0.0, min(1.0, 1.0 - (y_m + half) / side_m))
    col = int(round(u * (size - 1)))
    row = int(round(v * (size - 1)))
    return float(heightmap_m[row, col])

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


def resolve_region(bbox: BBoxLL, target_size_m: float = 2000.0, heightmap_size: int = 2048) -> Region:
    # 1) Reproject corners to ITM
    xs, ys = _TO_ITM.transform(
        [bbox.west, bbox.east], [bbox.south, bbox.north]
    )
    cx = (xs[0] + xs[1]) / 2
    cy = (ys[0] + ys[1]) / 2

    # 2) Snap to a square of side `target_size_m` centred on the user's centroid
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

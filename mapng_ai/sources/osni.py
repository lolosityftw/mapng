"""OSNI 1 m / 10 m DTM source — manual-config scaffold.

OpenDataNI doesn't expose a clean tile API; downloads come as ZIPs of GeoTIFFs
per river basin. Until we build a scraper, this source supports:

    1. Drop OSNI DTM GeoTIFFs into `assets/osni/dtm/`
    2. The pipeline auto-discovers any tile that intersects the requested bbox
       (uses each file's CRS + bounds via rasterio)
    3. Stitches and reprojects to lat/lon for the heightmap stage

Falls back gracefully (returns False from `covers()`) when no tiles cover the
bbox, so the coverage router moves on to Terrarium.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import rasterio
from rasterio.warp import transform_bounds, reproject, calculate_default_transform
from rasterio.enums import Resampling

from mapng_ai import config
from mapng_ai.sources.base import BBoxLL, ElevationTile


_OSNI_DIR = config.ROOT / "assets" / "osni" / "dtm"


# Rough Northern Ireland bbox — used to short-circuit auto-download
# probes when the user has picked a bbox outside NI (where OSNI has no
# coverage anyway).
def _likely_in_ni(bbox: BBoxLL) -> bool:
    cx = (bbox.west + bbox.east) / 2
    cy = (bbox.south + bbox.north) / 2
    return -8.4 < cx < -5.3 and 53.9 < cy < 55.4


class OSNISource:
    name = "OSNI 1m DTM (local tiles)"
    native_resolution_m = 1.0

    def __init__(self, tile_dir: Path = _OSNI_DIR) -> None:
        self.tile_dir = tile_dir

    def _candidate_tiles(self, bbox: BBoxLL) -> list[Path]:
        if not self.tile_dir.exists():
            return []
        out: list[Path] = []
        for p in sorted(self.tile_dir.glob("*.tif")):
            try:
                with rasterio.open(p) as ds:
                    bounds = transform_bounds(ds.crs, "EPSG:4326",
                                              *ds.bounds, densify_pts=21)
                    w, s, e, n = bounds
                    if e < bbox.west or w > bbox.east or n < bbox.south or s > bbox.north:
                        continue
                out.append(p)
            except Exception:
                continue
        return out

    async def covers(self, bbox: BBoxLL) -> bool:
        # Local tiles win immediately. Otherwise we lazily try the auto-
        # downloader and treat success as "we cover this bbox" so the
        # router prefers OSNI over Terrarium for the rest of the run.
        if self._candidate_tiles(bbox):
            return True
        # Centre roughly inside Northern Ireland? Skip the network probe
        # if the user is asking for somewhere outside NI's footprint.
        if not _likely_in_ni(bbox):
            return False
        try:
            from mapng_ai.sources.osni_download import auto_fetch_dtm
            path = await auto_fetch_dtm(bbox)
        except Exception:
            return False
        return path is not None

    async def fetch(self, bbox: BBoxLL) -> ElevationTile:
        tiles = self._candidate_tiles(bbox)
        if not tiles:
            # Race-safe: covers() may have already triggered a download.
            try:
                from mapng_ai.sources.osni_download import auto_fetch_dtm
                downloaded = await auto_fetch_dtm(bbox)
            except Exception:
                downloaded = None
            if downloaded is None:
                raise RuntimeError(
                    f"No OSNI tiles cover bbox and auto-download failed; "
                    f"drop GeoTIFFs in {self.tile_dir} or set "
                    f"MAPNG_OSNI_IMAGE_SERVER."
                )
            tiles = [downloaded]

        # Mosaic into EPSG:4326 at the union of all tiles' coverage
        # Simple approach: stack each tile reprojected at native resolution
        west, south, east, north = bbox.west, bbox.south, bbox.east, bbox.north
        # 0.0001° ≈ 11 m at NI latitudes — fine grid for mosaicing
        deg_per_px = 0.00001  # ~1.1 m
        cols = int((east - west) / deg_per_px) + 1
        rows = int((north - south) / deg_per_px) + 1
        cols = min(cols, 4096)
        rows = min(rows, 4096)

        out = np.full((rows, cols), -32768.0, dtype=np.float32)
        from rasterio.transform import from_bounds
        dst_transform = from_bounds(west, south, east, north, cols, rows)

        for p in tiles:
            with rasterio.open(p) as ds:
                arr = ds.read(1).astype(np.float32)
                src_transform = ds.transform
                src_crs = ds.crs
                tmp = np.full((rows, cols), -32768.0, dtype=np.float32)
                reproject(
                    source=arr, destination=tmp,
                    src_transform=src_transform, src_crs=src_crs,
                    dst_transform=dst_transform, dst_crs="EPSG:4326",
                    resampling=Resampling.bilinear,
                    src_nodata=ds.nodata, dst_nodata=-32768.0,
                )
                # Merge: replace where tmp has data
                mask = tmp != -32768.0
                out[mask] = tmp[mask]

        return ElevationTile(
            elevations_m=out,
            bbox_4326=BBoxLL(west=west, south=south, east=east, north=north),
        )

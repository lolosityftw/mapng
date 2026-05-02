"""OSNI DTM auto-downloader.

OpenDataNI hosts the Bare Earth 1 m DTM as ArcGIS Image services and as
zipped GeoTIFFs. This module tries the ArcGIS Image service approach
first (one HTTP call gets the exact tile we need), and falls back to a
pluggable URL list overridable by the `MAPNG_OSNI_IMAGE_SERVER` env var.

Result: a GeoTIFF placed in `assets/osni/dtm/auto/` that the existing
`OSNISource` discovers automatically the next time it scans.

If everything fails the function returns `None` and the coverage router
falls back to Terrarium 9 m. A clear log line tells the user to either
set `MAPNG_OSNI_IMAGE_SERVER` or drop tiles in `assets/osni/dtm/`.
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

import httpx
from pyproj import Transformer

from mapng_ai import config
from mapng_ai.sources.base import BBoxLL


log = logging.getLogger(__name__)


# Candidate ArcGIS Image services for the OSNI Bare Earth DTM. First
# one to return a usable GeoTIFF wins. The exact public URL has shifted
# over the years; an env var override is the long-term escape hatch.
_DEFAULT_CANDIDATES = (
    "https://services.spatialni.gov.uk/arcgis/rest/services/"
    "LPSCorporateBaseDatasets/OSNI_Open_Data_1m_DTM/ImageServer",

    "https://services.spatialni.gov.uk/arcgis/rest/services/"
    "OpenData/OSNI_Open_Data_1m_DTM/ImageServer",

    "https://services.spatialni.gov.uk/arcgis/rest/services/"
    "DTM_LiDAR/OSNI_OD_LiDAR_DTM_1m/ImageServer",

    # 5 m fallback if 1 m isn't reachable — still 2× sharper than Terrarium.
    "https://services.spatialni.gov.uk/arcgis/rest/services/"
    "LPSCorporateBaseDatasets/OSNI_Open_Data_5m_DTM/ImageServer",
)


# ITM (EPSG:2157) is what BeamNG-friendly NI levels use; image services
# accept any spatial reference so we ask for output in ITM directly to
# avoid an extra reproject.
_ITM_FROM_LL = Transformer.from_crs("EPSG:4326", "EPSG:2157", always_xy=True)


def _bbox_ll_to_itm_corners(bbox: BBoxLL) -> tuple[float, float, float, float]:
    xs, ys = _ITM_FROM_LL.transform(
        [bbox.west, bbox.east], [bbox.south, bbox.north]
    )
    return min(xs), min(ys), max(xs), max(ys)


def _candidate_urls() -> list[str]:
    override = os.environ.get("MAPNG_OSNI_IMAGE_SERVER", "").strip()
    if override:
        return [override.rstrip("/")] + list(_DEFAULT_CANDIDATES)
    return list(_DEFAULT_CANDIDATES)


def _cache_path(bbox: BBoxLL) -> Path:
    name = (f"auto_{bbox.west:.5f}_{bbox.south:.5f}_"
            f"{bbox.east:.5f}_{bbox.north:.5f}.tif")
    return config.ROOT / "assets" / "osni" / "dtm" / "auto" / name


async def auto_fetch_dtm(bbox: BBoxLL, *, pixel_size_m: float = 1.0,
                         max_pixels: int = 4096) -> Path | None:
    """Try to download a DTM GeoTIFF for `bbox`. Returns the cached path
    or None if every candidate URL fails.
    """
    out = _cache_path(bbox)
    if out.exists() and out.stat().st_size > 1024:
        return out

    out.parent.mkdir(parents=True, exist_ok=True)
    xmin, ymin, xmax, ymax = _bbox_ll_to_itm_corners(bbox)
    w_m = xmax - xmin
    h_m = ymax - ymin
    width_px  = min(max_pixels, max(64, int(round(w_m / pixel_size_m))))
    height_px = min(max_pixels, max(64, int(round(h_m / pixel_size_m))))

    headers = {
        "User-Agent": "mapng-ai/0.1 (research)",
        "Accept": "image/tiff, application/octet-stream;q=0.9, */*;q=0.5",
    }
    timeout = httpx.Timeout(connect=10.0, read=120.0, write=30.0, pool=10.0)

    async with httpx.AsyncClient(timeout=timeout, headers=headers,
                                  follow_redirects=True) as client:
        for base in _candidate_urls():
            url = f"{base}/exportImage"
            params = {
                "bbox": f"{xmin},{ymin},{xmax},{ymax}",
                "bboxSR": "2157",
                "imageSR": "2157",
                "size": f"{width_px},{height_px}",
                "format": "tiff",
                "pixelType": "F32",
                "noData": "-32768",
                "interpolation": "RSP_BilinearInterpolation",
                "f": "image",
            }
            try:
                r = await client.get(url, params=params)
            except Exception as exc:
                log.warning("OSNI auto-fetch %s: %s", base, exc)
                continue
            if r.status_code != 200:
                log.info("OSNI auto-fetch %s → HTTP %d", base, r.status_code)
                continue
            ctype = r.headers.get("content-type", "")
            body = r.content
            # ArcGIS sometimes replies 200 with a JSON error payload — guard.
            if "json" in ctype.lower() or body[:1] == b"{":
                log.info("OSNI auto-fetch %s returned JSON (likely error)", base)
                continue
            if len(body) < 1024:
                log.info("OSNI auto-fetch %s body too small (%d B)", base, len(body))
                continue
            out.write_bytes(body)
            log.info("OSNI auto-fetch hit %s — saved %s (%d bytes)",
                     base, out.name, len(body))
            return out
    log.warning(
        "OSNI auto-fetch: every candidate URL failed. "
        "Set MAPNG_OSNI_IMAGE_SERVER to a working ImageServer URL, "
        "or drop GeoTIFFs in assets/osni/dtm/."
    )
    return None


def auto_fetch_dtm_sync(bbox: BBoxLL, **kwargs) -> Path | None:
    return asyncio.run(auto_fetch_dtm(bbox, **kwargs))

"""Esri World Imagery — global high-res aerial imagery, no API key.

Used for:
  - the Three.js preview's terrain diffuse texture (massive visual upgrade)
  - per-pixel HSV classification (Phase 4 quality boost)

Tile URL: https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}

Esri's licence permits non-commercial use for previews/research with attribution
("Esri, Maxar, Earthstar Geographics, and the GIS User Community"). For
commercial / production use a paid tier or alternate source is required.
"""
from __future__ import annotations

import asyncio
import io
from dataclasses import dataclass

import httpx
import mercantile
import numpy as np
from PIL import Image

from mapng_ai.sources.base import BBoxLL


ESRI_URL = "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"
TILE_SIZE = 256
DEFAULT_ZOOM = 17   # ~0.6 m/px at NI latitudes — drastic detail


@dataclass(frozen=True)
class ImageryTile:
    rgb: np.ndarray             # (rows, cols, 3) uint8
    bbox_4326: BBoxLL
    zoom: int


class EsriSource:
    name = "Esri World Imagery"

    def __init__(self, zoom: int = DEFAULT_ZOOM, concurrency: int = 16) -> None:
        self.zoom = zoom
        self._sem = asyncio.Semaphore(concurrency)

    async def fetch(self, bbox: BBoxLL) -> ImageryTile:
        tiles = list(
            mercantile.tiles(bbox.west, bbox.south, bbox.east, bbox.north, [self.zoom])
        )
        if not tiles:
            raise RuntimeError(f"No Esri tiles intersect {bbox}")
        xs = [t.x for t in tiles]
        ys = [t.y for t in tiles]
        x_min, x_max = min(xs), max(xs)
        y_min, y_max = min(ys), max(ys)
        cols = (x_max - x_min + 1) * TILE_SIZE
        rows = (y_max - y_min + 1) * TILE_SIZE
        out = np.zeros((rows, cols, 3), dtype=np.uint8)

        async with httpx.AsyncClient(
            timeout=30.0,
            headers={"User-Agent": "mapng-ai/0.1 (research/preview)",
                     "Referer": "https://www.arcgis.com/"},
        ) as client:
            async def get(t: mercantile.Tile):
                async with self._sem:
                    r = await client.get(ESRI_URL.format(z=t.z, x=t.x, y=t.y))
                    r.raise_for_status()
                    img = np.array(Image.open(io.BytesIO(r.content)).convert("RGB"))
                    return t, img

            results = await asyncio.gather(*(get(t) for t in tiles))

        for t, img in results:
            ry = (t.y - y_min) * TILE_SIZE
            rx = (t.x - x_min) * TILE_SIZE
            out[ry:ry + TILE_SIZE, rx:rx + TILE_SIZE] = img

        nw = mercantile.ul(mercantile.Tile(x_min, y_min, self.zoom))
        se = mercantile.ul(mercantile.Tile(x_max + 1, y_max + 1, self.zoom))
        covered = BBoxLL(west=nw.lng, south=se.lat, east=se.lng, north=nw.lat)
        return ImageryTile(rgb=out, bbox_4326=covered, zoom=self.zoom)


_DEFAULT: EsriSource | None = None


def default_imagery_source() -> EsriSource:
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = EsriSource()
    return _DEFAULT

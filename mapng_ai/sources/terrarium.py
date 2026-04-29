"""AWS Terrarium global elevation tiles — the always-on fallback.

Terrarium tiles are PNG-encoded elevations where:
    elevation_m = R * 256 + G + B / 256 - 32768

Tiles served from https://elevation-tiles-prod.s3.amazonaws.com/terrarium/{z}/{x}/{y}.png
under a CC-BY licence (Mapzen, hosted by AWS).
"""
from __future__ import annotations

import asyncio
import io
import math

import httpx
import mercantile
import numpy as np
from PIL import Image

from mapng_ai.sources.base import BBoxLL, ElevationSource, ElevationTile

TERRARIUM_URL = "https://elevation-tiles-prod.s3.amazonaws.com/terrarium/{z}/{x}/{y}.png"
TILE_SIZE = 256
DEFAULT_ZOOM = 14   # ~9.5 m/px at NI latitudes — sharper than the 30 m product


def _decode_terrarium(png_bytes: bytes) -> np.ndarray:
    """Decode a Terrarium PNG to float32 metres."""
    img = np.array(Image.open(io.BytesIO(png_bytes)).convert("RGB"), dtype=np.float32)
    r, g, b = img[..., 0], img[..., 1], img[..., 2]
    return r * 256.0 + g + b / 256.0 - 32768.0


class TerrariumSource:
    name = "Terrarium 30m (global)"
    native_resolution_m = 30.0

    def __init__(self, zoom: int = DEFAULT_ZOOM, concurrency: int = 16) -> None:
        self.zoom = zoom
        self._sem = asyncio.Semaphore(concurrency)

    async def covers(self, bbox: BBoxLL) -> bool:
        return True  # global

    async def fetch(self, bbox: BBoxLL) -> ElevationTile:
        # Tiles intersecting the bbox at our chosen zoom
        tiles = list(
            mercantile.tiles(bbox.west, bbox.south, bbox.east, bbox.north, [self.zoom])
        )
        if not tiles:
            raise RuntimeError(f"No Terrarium tiles cover bbox {bbox}")

        xs = [t.x for t in tiles]
        ys = [t.y for t in tiles]
        x_min, x_max = min(xs), max(xs)
        y_min, y_max = min(ys), max(ys)
        cols = (x_max - x_min + 1) * TILE_SIZE
        rows = (y_max - y_min + 1) * TILE_SIZE

        out = np.full((rows, cols), -32768.0, dtype=np.float32)

        async with httpx.AsyncClient(
            timeout=30.0, headers={"User-Agent": "mapng-ai/0.1"}
        ) as client:
            async def get(t: mercantile.Tile) -> tuple[mercantile.Tile, np.ndarray]:
                async with self._sem:
                    url = TERRARIUM_URL.format(z=t.z, x=t.x, y=t.y)
                    r = await client.get(url)
                    r.raise_for_status()
                    return t, _decode_terrarium(r.content)

            results = await asyncio.gather(*(get(t) for t in tiles))

        for t, arr in results:
            ry = (t.y - y_min) * TILE_SIZE
            rx = (t.x - x_min) * TILE_SIZE
            out[ry : ry + TILE_SIZE, rx : rx + TILE_SIZE] = arr

        # Compute the actual covered bbox (slightly larger than requested)
        nw = mercantile.ul(mercantile.Tile(x_min, y_min, self.zoom))
        se_tile = mercantile.Tile(x_max, y_max, self.zoom)
        se = mercantile.ul(mercantile.Tile(se_tile.x + 1, se_tile.y + 1, self.zoom))
        covered = BBoxLL(west=nw.lng, south=se.lat, east=se.lng, north=nw.lat)

        return ElevationTile(elevations_m=out, bbox_4326=covered)


_DEFAULT_SOURCE: TerrariumSource | None = None


def default_source() -> ElevationSource:
    global _DEFAULT_SOURCE
    if _DEFAULT_SOURCE is None:
        _DEFAULT_SOURCE = TerrariumSource()
    return _DEFAULT_SOURCE

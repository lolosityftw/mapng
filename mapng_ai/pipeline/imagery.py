"""Stage 2 (extension) — fetch + reproject aerial imagery.

Produces an ITM-aligned PNG that's used both as the preview-pane terrain
texture and as the input to the HSV-based segmentation."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.transform import from_bounds
from rasterio.warp import reproject

from mapng_ai.pipeline.region import Region
from mapng_ai.sources.esri import ImageryTile


@dataclass(frozen=True)
class ImageryResult:
    rgb: np.ndarray            # (size, size, 3) uint8 — ITM-aligned
    sat_png_path: Path         # in level + preview consumes this
    side_m: float
    zoom: int


def reproject_imagery(tile: ImageryTile, region: Region, out_dir: Path,
                      target_size: int = 2048) -> ImageryResult:
    out_dir.mkdir(parents=True, exist_ok=True)

    src_h, src_w = tile.rgb.shape[:2]
    src_b = tile.bbox_4326
    src_transform = from_bounds(
        src_b.west, src_b.south, src_b.east, src_b.north, src_w, src_h
    )

    target_b = region.working_itm
    dst_transform = from_bounds(
        target_b.west, target_b.south, target_b.east, target_b.north,
        target_size, target_size,
    )
    dst = np.zeros((target_size, target_size, 3), dtype=np.uint8)
    for ch in range(3):
        reproject(
            source=tile.rgb[..., ch],
            destination=dst[..., ch],
            src_transform=src_transform,
            src_crs="EPSG:4326",
            dst_transform=dst_transform,
            dst_crs="EPSG:2157",
            resampling=Resampling.lanczos,
        )

    sat_path = out_dir / "satellite.png"
    from PIL import Image
    Image.fromarray(dst).save(sat_path, optimize=True)
    return ImageryResult(rgb=dst, sat_png_path=sat_path, side_m=region.side_m, zoom=tile.zoom)

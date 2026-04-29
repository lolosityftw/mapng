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
    normal_png_path: Path      # micro-bump normal map derived from imagery
    side_m: float
    zoom: int


def _normal_map_from_rgb(rgb: np.ndarray, *, strength: float = 2.5) -> np.ndarray:
    """Cheap normal map derived from imagery brightness.

    Maps brightness gradients to surface normals — bushes, hedge shadows, even
    field-edge tracks become subtle 3D bumps when lit. Output: (H,W,3) uint8
    in standard normal-map encoding (R=X+, G=Y+, B=Z+, mid grey = flat).
    """
    from scipy.ndimage import gaussian_filter
    grey = (0.299 * rgb[..., 0] + 0.587 * rgb[..., 1] + 0.114 * rgb[..., 2]).astype(np.float32) / 255.0
    grey = gaussian_filter(grey, sigma=1.0)
    gx = np.zeros_like(grey)
    gy = np.zeros_like(grey)
    gx[:, 1:-1] = (grey[:, 2:] - grey[:, :-2]) * 0.5
    gy[1:-1, :] = (grey[2:, :] - grey[:-2, :]) * 0.5
    nx = -gx * strength
    ny = -gy * strength
    nz = np.ones_like(grey)
    norm = np.sqrt(nx * nx + ny * ny + nz * nz) + 1e-6
    nx, ny, nz = nx / norm, ny / norm, nz / norm
    out = np.stack([
        ((nx * 0.5 + 0.5) * 255).clip(0, 255),
        ((ny * 0.5 + 0.5) * 255).clip(0, 255),
        ((nz * 0.5 + 0.5) * 255).clip(0, 255),
    ], axis=-1).astype(np.uint8)
    return out


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

    normal_path = out_dir / "satellite_normal.png"
    normal = _normal_map_from_rgb(dst)
    Image.fromarray(normal).save(normal_path, optimize=True)

    return ImageryResult(
        rgb=dst, sat_png_path=sat_path, normal_png_path=normal_path,
        side_m=region.side_m, zoom=tile.zoom,
    )

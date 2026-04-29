"""Stage 3 — heightmap build.

Stitches the source ElevationTile, fills any NoData voids, reprojects to ITM,
and writes a square 16-bit PNG (the format the BeamNG `.ter` writer wants)
plus a GeoTIFF for QGIS sanity-checking.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.transform import from_bounds
from rasterio.warp import reproject

from mapng_ai.pipeline.region import Region
from mapng_ai.sources.base import ElevationTile


@dataclass(frozen=True)
class HeightmapResult:
    elevations_m: np.ndarray            # float32, (size, size), ITM-aligned
    min_m: float
    max_m: float
    geotiff_path: Path
    png16_path: Path
    preview_png_path: Path              # 8-bit visualisation for the browser


# ---------------------------------------------------------------------------
# Fill + relax — ported from MapNG functions.txt
# ---------------------------------------------------------------------------
def expand_fill(
    arr: np.ndarray,
    no_data_mask: np.ndarray,
    radius: int = 3,
    max_passes: int = 64,
) -> np.ndarray:
    """Iteratively replace NoData pixels with the mean of their valid neighbours.

    Returns a boolean mask of which pixels were filled (so `relax_filled` can
    smooth those without disturbing the originals).
    """
    out = arr.copy()
    filled_mask = np.zeros_like(no_data_mask, dtype=bool)
    h, w = out.shape
    for _ in range(max_passes):
        any_filled = False
        # Identify NoData pixels still empty
        ys, xs = np.where(no_data_mask & ~filled_mask)
        if ys.size == 0:
            break
        for y, x in zip(ys, xs):
            y0, y1 = max(0, y - radius), min(h, y + radius + 1)
            x0, x1 = max(0, x - radius), min(w, x + radius + 1)
            window = out[y0:y1, x0:x1]
            mask_w = no_data_mask[y0:y1, x0:x1] & ~filled_mask[y0:y1, x0:x1]
            valid = window[~mask_w]
            valid = valid[np.isfinite(valid)]
            if valid.size:
                out[y, x] = valid.mean()
                filled_mask[y, x] = True
                any_filled = True
        if not any_filled:
            break
    return out, filled_mask


def relax_filled(
    arr: np.ndarray, filled_mask: np.ndarray, iterations: int = 80, tension: float = 0.5
) -> np.ndarray:
    """Bilaplacian relaxation on *only* the filled pixels.

    Tension blends biharmonic curvature with simple Laplacian to suppress
    Gibbs-style overshoots near hole edges. Ported from MapNG functions.txt.
    """
    out = arr.copy()
    if not filled_mask.any():
        return out
    h, w = out.shape

    def at(x, y):
        return out[max(0, min(h - 1, y)), max(0, min(w - 1, x))]

    ys, xs = np.where(filled_mask)
    for _ in range(iterations):
        updated = False
        for y, x in zip(ys, xs):
            cur = out[y, x]
            n1 = at(x - 1, y) + at(x + 1, y) + at(x, y - 1) + at(x, y + 1)
            n_diag = at(x - 1, y - 1) + at(x + 1, y - 1) + at(x - 1, y + 1) + at(x + 1, y + 1)
            n2 = at(x - 2, y) + at(x + 2, y) + at(x, y - 2) + at(x, y + 2)
            biharmonic = (8 * n1 - 2 * n_diag - n2) / 20.0
            laplacian = n1 / 4.0
            new = biharmonic * (1 - tension) + laplacian * tension
            if abs(new - cur) > 1e-4:
                out[y, x] = new
                updated = True
        if not updated:
            break
    return out


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------
def build_heightmap(tile: ElevationTile, region: Region, out_dir: Path) -> HeightmapResult:
    out_dir.mkdir(parents=True, exist_ok=True)

    src = tile.elevations_m.astype(np.float32)

    # 1) Fill NoData (Terrarium tiles rarely have them, but other sources will)
    no_data_mask = (src == tile.no_data_value) | ~np.isfinite(src)
    if no_data_mask.any():
        src, filled = expand_fill(src, no_data_mask)
        if filled.any():
            src = relax_filled(src, filled)

    # 2) Reproject from EPSG:4326 → EPSG:2157 (ITM), clipped to the working square
    src_h, src_w = src.shape
    src_bbox = tile.bbox_4326
    src_transform = from_bounds(
        src_bbox.west, src_bbox.south, src_bbox.east, src_bbox.north, src_w, src_h
    )

    target_size = region.heightmap_size
    target_bbox = region.working_itm
    dst_transform = from_bounds(
        target_bbox.west, target_bbox.south, target_bbox.east, target_bbox.north,
        target_size, target_size,
    )
    dst = np.empty((target_size, target_size), dtype=np.float32)

    reproject(
        source=src,
        destination=dst,
        src_transform=src_transform,
        src_crs="EPSG:4326",
        dst_transform=dst_transform,
        dst_crs="EPSG:2157",
        resampling=Resampling.bilinear,
    )

    # 3) Write artefacts
    min_m = float(dst.min())
    max_m = float(dst.max())

    geotiff = out_dir / "terrain.tif"
    with rasterio.open(
        geotiff, "w",
        driver="GTiff", height=target_size, width=target_size, count=1,
        dtype="float32", crs="EPSG:2157", transform=dst_transform,
        compress="lzw", tiled=True,
    ) as ds:
        ds.write(dst, 1)

    # 16-bit PNG: normalised to the actual height range
    png16 = out_dir / "heightmap16.png"
    rng = max(max_m - min_m, 1.0)
    quant = ((dst - min_m) / rng * 65535.0).clip(0, 65535).astype(np.uint16)
    # PIL handles 16-bit greyscale via mode 'I;16'
    from PIL import Image
    Image.fromarray(quant, mode="I;16").save(png16)

    # 8-bit preview (browser-friendly)
    preview = out_dir / "heightmap8.png"
    quant8 = ((dst - min_m) / rng * 255.0).clip(0, 255).astype(np.uint8)
    Image.fromarray(quant8, mode="L").save(preview)

    return HeightmapResult(
        elevations_m=dst,
        min_m=min_m,
        max_m=max_m,
        geotiff_path=geotiff,
        png16_path=png16,
        preview_png_path=preview,
    )

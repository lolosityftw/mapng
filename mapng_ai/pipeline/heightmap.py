"""Stage 3 — heightmap build.

Stitches the source ElevationTile, fills any NoData voids, reprojects to ITM,
and writes a square 16-bit PNG (the format the BeamNG `.ter` writer wants)
plus a GeoTIFF for QGIS sanity-checking.

Cache: results are content-addressed by (bbox + side_m + heightmap_size),
so re-running the same bbox skips fetch+stitch+reproject entirely.
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
    cache_hit: bool = False


def _cache_key(region: Region) -> str:
    import hashlib
    b = region.working_itm
    payload = f"{b.west:.3f}|{b.south:.3f}|{b.east:.3f}|{b.north:.3f}|{region.heightmap_size}"
    return hashlib.sha1(payload.encode()).hexdigest()[:16]


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
def _heightmap_cache_dir() -> Path:
    from mapng_ai import config
    d = config.CACHE_DIR / "heightmap"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _try_load_cached(region: Region, out_dir: Path) -> HeightmapResult | None:
    cache = _heightmap_cache_dir() / _cache_key(region)
    if not (cache / "meta.json").exists():
        return None
    try:
        import json, shutil
        meta = json.loads((cache / "meta.json").read_text(encoding="utf-8"))
        # Copy from cache → this job's out_dir so artifact URLs resolve
        out_dir.mkdir(parents=True, exist_ok=True)
        for name in ("terrain.tif", "heightmap16.png", "heightmap8.png"):
            src = cache / name
            dst = out_dir / name
            if not dst.exists():
                shutil.copyfile(src, dst)
        # Reload elevations array for downstream stages
        with rasterio.open(out_dir / "terrain.tif") as ds:
            arr = ds.read(1).astype(np.float32)
        return HeightmapResult(
            elevations_m=arr,
            min_m=float(meta["min_m"]),
            max_m=float(meta["max_m"]),
            geotiff_path=out_dir / "terrain.tif",
            png16_path=out_dir / "heightmap16.png",
            preview_png_path=out_dir / "heightmap8.png",
            cache_hit=True,
        )
    except Exception:
        return None


def _save_cached(result: HeightmapResult, region: Region) -> None:
    import json, shutil
    cache = _heightmap_cache_dir() / _cache_key(region)
    cache.mkdir(parents=True, exist_ok=True)
    for src in (result.geotiff_path, result.png16_path, result.preview_png_path):
        dst = cache / src.name
        if not dst.exists():
            shutil.copyfile(src, dst)
    (cache / "meta.json").write_text(
        json.dumps({"min_m": result.min_m, "max_m": result.max_m}),
        encoding="utf-8",
    )


def build_heightmap(tile: ElevationTile, region: Region, out_dir: Path) -> HeightmapResult:
    cached = _try_load_cached(region, out_dir)
    if cached is not None:
        return cached

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

    result = HeightmapResult(
        elevations_m=dst,
        min_m=min_m,
        max_m=max_m,
        geotiff_path=geotiff,
        png16_path=png16,
        preview_png_path=preview,
    )
    try:
        _save_cached(result, region)
    except Exception:
        pass
    return result

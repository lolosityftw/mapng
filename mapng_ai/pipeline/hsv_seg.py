"""Image-driven land cover classification.

This is a colour-based classifier — *not* a neural net — but it operates on
real aerial imagery so it picks up driveways, paths, mud patches, individual
trees, and other detail that OSM polygons don't carry.

Combine with `classmap.build_class_map` like this:
    osm_map = build_class_map(...)
    hsv_map = classify_imagery(...)
    fused   = fuse_segmentation(osm_map, hsv_map)

The fuser lets OSM win for *strong* signals (asphalt roads, water polygons)
and uses the imagery to refine the *weakly-classified* default-pasture
background into pasture / lawn / earth / forest / etc.
"""
from __future__ import annotations

import numpy as np
from scipy.ndimage import gaussian_filter

from mapng_ai.pipeline.classmap import CLASSES, DEFAULT_CLASS


# Class IDs (must match classmap.CLASSES)
ASPHALT, CONCRETE, LAWN, PASTURE, EARTH, GRAVEL, WATER, FOREST = range(8)


def _rgb_to_hsv(rgb: np.ndarray) -> np.ndarray:
    """Vectorised RGB→HSV. Input uint8 (H, W, 3). Output float32 (H, W, 3) in [0,1]."""
    r = rgb[..., 0].astype(np.float32) / 255.0
    g = rgb[..., 1].astype(np.float32) / 255.0
    b = rgb[..., 2].astype(np.float32) / 255.0
    cmax = np.maximum(np.maximum(r, g), b)
    cmin = np.minimum(np.minimum(r, g), b)
    delta = cmax - cmin

    h = np.zeros_like(cmax)
    safe = delta > 1e-6
    rmax = (cmax == r) & safe
    gmax = (cmax == g) & safe
    bmax = (cmax == b) & safe
    h[rmax] = ((g[rmax] - b[rmax]) / delta[rmax]) % 6
    h[gmax] = ((b[gmax] - r[gmax]) / delta[gmax]) + 2
    h[bmax] = ((r[bmax] - g[bmax]) / delta[bmax]) + 4
    h = h / 6.0
    s = np.where(cmax > 1e-6, delta / cmax, 0.0)
    v = cmax
    return np.stack([h, s, v], axis=-1).astype(np.float32)


def classify_imagery(rgb: np.ndarray) -> np.ndarray:
    """Classify each pixel of the aerial RGB image into our 8 land cover classes.

    Heuristics tuned for NI rural / mixed Esri imagery — bias toward
    pasture/forest/asphalt as the dominant categories.
    """
    hsv = _rgb_to_hsv(rgb)
    h, s, v = hsv[..., 0], hsv[..., 1], hsv[..., 2]
    out = np.full(rgb.shape[:2], DEFAULT_CLASS, dtype=np.uint8)

    # Smooth value/saturation a touch to suppress JPEG noise
    v_s = gaussian_filter(v, sigma=0.6)
    s_s = gaussian_filter(s, sigma=0.6)

    is_green = (h > 0.20) & (h < 0.45) & (s_s > 0.10)
    is_blue  = (h > 0.52) & (h < 0.70) & (s_s > 0.20) & (v_s > 0.20)
    is_brown = ((h < 0.12) | (h > 0.92)) & (s_s > 0.18) & (v_s < 0.55)
    is_grey  = (s_s < 0.10)

    # Forest = dark + heavily saturated green (tree canopies cast shadows so
    # they're notably darker than pasture from above).
    forest = is_green & (v_s < 0.32) & (s_s > 0.30)
    # Pasture = the broad green default for everything green-ish that isn't
    # clearly forest. Lawn is only really bright green (rare in rural NI).
    pasture = is_green & ~forest
    lawn = pasture & (v_s > 0.62) & (s_s > 0.30)
    pasture = pasture & ~lawn

    # Roads / driveways: very dark grey
    asphalt = is_grey & (v_s < 0.28)
    # Buildings, paved areas: mid grey
    concrete = is_grey & (v_s >= 0.28) & (v_s < 0.62)
    # Gravel = warm light grey
    gravel = is_grey & (v_s >= 0.62)

    # Earth = brown, low value
    earth = is_brown & ~is_green

    out[forest]  = FOREST
    out[pasture] = PASTURE
    out[lawn]    = LAWN
    out[asphalt] = ASPHALT
    out[concrete] = CONCRETE
    out[gravel]  = GRAVEL
    out[earth]   = EARTH
    out[is_blue] = WATER

    return out


# OSM-trusted classes — these always win over imagery
_OSM_TRUSTED = frozenset({ASPHALT, CONCRETE, WATER})


def fuse_segmentation(osm_map: np.ndarray, image_map: np.ndarray) -> np.ndarray:
    """OSM wins for trusted classes (roads, buildings, water polygons).
    Imagery refines the rest (pasture / forest / earth / etc.)."""
    if osm_map.shape != image_map.shape:
        raise ValueError(f"shape mismatch: osm {osm_map.shape} vs image {image_map.shape}")
    out = image_map.copy()
    osm_trusted_mask = np.isin(osm_map, list(_OSM_TRUSTED))
    out[osm_trusted_mask] = osm_map[osm_trusted_mask]
    return out

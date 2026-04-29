"""Procedural per-class ground textures.

Goal: stop shipping flat colour swatches as the diffuse maps. The full PBR
upgrade will plug in real CC0 textures from Poly Haven (Phase 4 polish), but
in the meantime we generate plausible 2048² diffuse + roughness + normal
images deterministically. Cached on disk so re-runs are instant.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image
from scipy.ndimage import gaussian_filter

from mapng_ai import config


TEX_SIZE = 1024  # 2048 is overkill for one-tile-per-detail; 1024 is plenty
SEED = 0xC00C    # Cookstown ;)


def _noise(shape, sigma, seed):
    rng = np.random.default_rng(seed)
    base = rng.standard_normal(shape).astype(np.float32)
    out = gaussian_filter(base, sigma=sigma)
    out -= out.min()
    rng_amp = max(out.max() - out.min(), 1e-6)
    return out / rng_amp


def _fbm(shape: tuple[int, int], scales: list[float], weights: list[float], seed: int) -> np.ndarray:
    """Fractional-brownian-motion-ish: weighted sum of multi-scale Gaussian noise."""
    out = np.zeros(shape, dtype=np.float32)
    for i, (s, w) in enumerate(zip(scales, weights)):
        out += w * _noise(shape, s, seed + i)
    out -= out.min()
    return out / max(out.max(), 1e-6)


def _tint(base_rgb: tuple[int, int, int], variation: tuple[int, int, int],
          mask: np.ndarray) -> np.ndarray:
    """Blend `mask` (0..1) between base and base+variation."""
    base = np.array(base_rgb, dtype=np.float32)
    delta = np.array(variation, dtype=np.float32)
    out = base[None, None, :] + mask[..., None] * delta[None, None, :]
    return np.clip(out, 0, 255)


# ---------------------------------------------------------------------------
# Per-class recipes
# ---------------------------------------------------------------------------
def _asphalt() -> np.ndarray:
    grain = _noise((TEX_SIZE, TEX_SIZE), sigma=0.7, seed=SEED + 1)
    coarse = _fbm((TEX_SIZE, TEX_SIZE), [3.0, 12.0, 40.0], [0.4, 0.4, 0.2], seed=SEED + 2)
    base = _tint((52, 52, 56), (24, 24, 24), 0.6 * coarse + 0.4 * grain)
    # Sparse light pebbles
    rng = np.random.default_rng(SEED + 3)
    pebbles = (rng.random((TEX_SIZE, TEX_SIZE)) > 0.998).astype(np.float32)
    pebbles = gaussian_filter(pebbles, 0.6)
    base += pebbles[..., None] * np.array([60, 60, 60], dtype=np.float32)
    return np.clip(base, 0, 255).astype(np.uint8)


def _concrete() -> np.ndarray:
    stains = _fbm((TEX_SIZE, TEX_SIZE), [16.0, 60.0], [0.6, 0.4], seed=SEED + 11)
    grain = _noise((TEX_SIZE, TEX_SIZE), sigma=0.6, seed=SEED + 12)
    return np.clip(_tint((175, 173, 168), (30, 30, 25), 0.7 * stains + 0.3 * grain), 0, 255).astype(np.uint8)


def _grass(base, variation, key_seed: int, mottling=2.0) -> np.ndarray:
    blade = _noise((TEX_SIZE, TEX_SIZE), sigma=0.7, seed=key_seed + 1)
    patches = _fbm((TEX_SIZE, TEX_SIZE), [6.0, 24.0, 80.0], [0.5, 0.3, 0.2], seed=key_seed + 2)
    return np.clip(_tint(base, variation, mottling * blade * 0.4 + patches), 0, 255).astype(np.uint8)


def _earth() -> np.ndarray:
    streaks = _fbm((TEX_SIZE, TEX_SIZE), [4.0, 16.0, 64.0], [0.4, 0.3, 0.3], seed=SEED + 31)
    grain = _noise((TEX_SIZE, TEX_SIZE), sigma=0.7, seed=SEED + 32)
    return np.clip(_tint((130, 100, 64), (50, 40, 25), 0.7 * streaks + 0.3 * grain), 0, 255).astype(np.uint8)


def _gravel() -> np.ndarray:
    grain = _noise((TEX_SIZE, TEX_SIZE), sigma=0.5, seed=SEED + 41)
    patches = _fbm((TEX_SIZE, TEX_SIZE), [2.0, 10.0], [0.6, 0.4], seed=SEED + 42)
    base = _tint((130, 120, 105), (45, 40, 30), 0.5 * grain + 0.5 * patches)
    rng = np.random.default_rng(SEED + 43)
    pebbles = (rng.random((TEX_SIZE, TEX_SIZE)) > 0.996).astype(np.float32)
    pebbles = gaussian_filter(pebbles, 0.8)
    base += pebbles[..., None] * np.array([45, 40, 35], dtype=np.float32)
    return np.clip(base, 0, 255).astype(np.uint8)


def _water() -> np.ndarray:
    waves = _fbm((TEX_SIZE, TEX_SIZE), [12.0, 48.0], [0.5, 0.5], seed=SEED + 51)
    return np.clip(_tint((48, 92, 138), (40, 50, 50), waves), 0, 255).astype(np.uint8)


def _forest() -> np.ndarray:
    canopy = _fbm((TEX_SIZE, TEX_SIZE), [2.0, 10.0, 40.0], [0.3, 0.4, 0.3], seed=SEED + 61)
    grain = _noise((TEX_SIZE, TEX_SIZE), sigma=0.6, seed=SEED + 62)
    base = _tint((45, 70, 32), (40, 50, 30), 0.65 * canopy + 0.35 * grain)
    return np.clip(base, 0, 255).astype(np.uint8)


_RECIPES = {
    "asphalt":  _asphalt,
    "concrete": _concrete,
    "lawn":     lambda: _grass((85, 140, 55),  (40, 60, 30), SEED + 21),
    "pasture":  lambda: _grass((115, 140, 70), (50, 50, 35), SEED + 22, mottling=2.5),
    "earth":    _earth,
    "gravel":   _gravel,
    "water":    _water,
    "forest":   _forest,
}


# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class GroundTexture:
    key: str
    diffuse_path: Path


def get_ground_texture(class_key: str) -> GroundTexture:
    """Return a cached PBR-ish diffuse texture for the named class."""
    cache_dir = config.CACHE_DIR / "textures"
    cache_dir.mkdir(parents=True, exist_ok=True)
    diffuse_path = cache_dir / f"{class_key}_diffuse.png"
    if not diffuse_path.exists():
        recipe = _RECIPES.get(class_key)
        if recipe is None:
            raise KeyError(f"No texture recipe for {class_key!r}")
        rgb = recipe()
        Image.fromarray(rgb).save(diffuse_path, optimize=True)
    return GroundTexture(key=class_key, diffuse_path=diffuse_path)

"""Stage 5 — material splatting (Phase 4).

Per spec §4.2:
    1. Take class map (size×size, integer)
    2. For each class generate a binary opacity mask
    3. Gaussian-blur (~1.5 px) so transitions feather
    4. Multiply by low-frequency Perlin-style noise
    5. Hard-paint roads on top (already done by classmap)
    6. Normalise so opacities sum to 1.0 per pixel
    7. Write each layer as a greyscale PNG

We also build a single combined diffuse-colour PNG (terrain.png) — the
weighted blend of each class's solid-colour swatch — so the BeamNG terrain
looks correct even before we add per-class PBR detail textures.
"""
from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image
from scipy.ndimage import gaussian_filter

from mapng_ai.pipeline.classmap import CLASSES, LandClass
from mapng_ai.pipeline.textures import get_ground_texture


_BLUR_SIGMA = 1.5
_NOISE_SCALE = 32.0     # texels — the broader the more "patchy" the variation
_NOISE_AMPLITUDE = 0.18  # ±18% of the layer's opacity


@dataclass(frozen=True)
class SplatLayer:
    cls: LandClass
    opacity_path: Path                  # 8-bit PNG, single channel
    diffuse_path: Path                  # diffuse tile (Poly Haven if present, else procedural)
    normal_path: Path | None = None     # only set when Poly Haven PBR is available
    roughness_path: Path | None = None
    source: str = "procedural"          # "polyhaven" or "procedural"
    coverage_pct: float = 0.0


@dataclass(frozen=True)
class SplatResult:
    layers: list[SplatLayer]
    combined_diffuse_path: Path  # blended preview texture
    layer_index_map: np.ndarray  # uint8, terrain-space (row 0 = SOUTH per .ter convention)


def _low_freq_noise(shape: tuple[int, int], scale: float, seed: int) -> np.ndarray:
    """Cheap stand-in for Perlin: blurred Gaussian white noise, normalised to ±1."""
    rng = np.random.default_rng(seed)
    base = rng.standard_normal(shape).astype(np.float32)
    return gaussian_filter(base, sigma=scale) * 6.0  # post-blur amplitude boost


def build_splat(class_map: np.ndarray, out_dir: Path, *, seed: int = 1) -> SplatResult:
    """class_map is in image space (row 0 = north). We return layers in image
    space too; the .ter writer will Y-flip the layer index map separately."""
    out_dir.mkdir(parents=True, exist_ok=True)
    h, w = class_map.shape
    if h != w:
        raise ValueError("class map must be square")
    size = h

    # 1+2) Per-class binary mask, blurred
    # 3) Multiply by low-frequency noise (positive only; clip ≥ 0)
    noise = 1.0 + _low_freq_noise((size, size), _NOISE_SCALE, seed) * _NOISE_AMPLITUDE
    noise = np.clip(noise, 0.4, 1.6)

    raw_layers: list[tuple[LandClass, np.ndarray]] = []
    for cid, cls in CLASSES.items():
        mask = (class_map == cid).astype(np.float32)
        if mask.sum() == 0:
            continue
        feathered = gaussian_filter(mask, sigma=_BLUR_SIGMA)
        weighted = feathered * noise
        raw_layers.append((cls, weighted))

    # Always include the default class even if not directly tagged, so background
    # has something — handled because pasture gets painted everywhere by classmap.

    # 6) Normalise per-pixel sum to 1
    stack = np.stack([l for _, l in raw_layers], axis=0)
    sums = stack.sum(axis=0, keepdims=True)
    sums = np.where(sums < 1e-6, 1.0, sums)
    norm = stack / sums

    # 7) Write layer PNGs + class swatch diffuse PNGs
    layers: list[SplatLayer] = []
    combined = np.zeros((size, size, 3), dtype=np.float32)

    for (cls, _), layer in zip(raw_layers, norm):
        opacity_8 = (layer * 255.0).clip(0, 255).astype(np.uint8)
        opacity_path = out_dir / f"opacity_{cls.key}.png"
        Image.fromarray(opacity_8, mode="L").save(opacity_path)

        # PBR tile — prefers real Poly Haven texture, falls back to procedural
        gt = get_ground_texture(cls.key)
        diffuse_path = gt.diffuse_path

        # Accumulate the combined preview
        combined += layer[..., None] * np.array(cls.color_rgb, dtype=np.float32)

        coverage = float((layer > 0.05).mean()) * 100
        layers.append(SplatLayer(
            cls=cls,
            opacity_path=opacity_path,
            diffuse_path=diffuse_path,
            normal_path=gt.normal_path,
            roughness_path=gt.roughness_path,
            source=gt.source,
            coverage_pct=coverage,
        ))

    combined_8 = combined.clip(0, 255).astype(np.uint8)
    combined_path = out_dir / "terrain_combined.png"
    Image.fromarray(combined_8).save(combined_path)

    # Layer index map: per pixel, which class has the highest weight?
    idx = np.argmax(stack, axis=0)
    cls_lookup = np.array([cls.id for cls, _ in raw_layers], dtype=np.uint8)
    layer_index_map = cls_lookup[idx]

    # .ter expects row 0 = SOUTH, so flip vertically
    layer_index_map_terrain_space = layer_index_map[::-1, :].copy()

    return SplatResult(
        layers=layers,
        combined_diffuse_path=combined_path,
        layer_index_map=layer_index_map_terrain_space,
    )

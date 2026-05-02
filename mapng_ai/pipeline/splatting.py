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

# Per-class detail-blend strength inside build_detailed_terrain. 1.0 means
# the satellite contributes nothing for that class; lower values let the
# Esri imagery bleed through. The satellite tends to bake autumn / brown
# bias into NI fields, so we push grass classes very high.
_DETAIL_BLEND_PER_CLASS: dict[str, float] = {
    "pasture":  0.98,
    "lawn":     0.97,
    "forest":   0.95,
    "earth":    0.85,
    "gravel":   0.85,
    "concrete": 0.85,
    "asphalt":  0.95,
    "water":    0.30,        # let the satellite show some river colour
}
_DETAIL_BLEND_DEFAULT = 0.92

# Per-field tint variation. Each Voronoi-style cell gets a small RGB
# multiplier so adjacent fields read as visibly different greens — the
# patchwork-quilt look you see flying over rural Ireland.
_FIELD_GRID = 28        # cells across the terrain (= ~70m fields on a 2km map)
_FIELD_SEED = 0xF1E1D


@dataclass(frozen=True)
class SplatLayer:
    cls: LandClass
    opacity_path: Path                  # 8-bit PNG, single channel
    diffuse_path: Path                  # diffuse tile (Poly Haven if present, else procedural)
    normal_path: Path | None = None     # only set when Poly Haven PBR is available
    roughness_path: Path | None = None
    source: str = "procedural"          # "polyhaven" or "procedural"
    coverage_pct: float = 0.0


@dataclass
class SplatResult:
    layers: list[SplatLayer]
    combined_diffuse_path: Path        # blended preview texture (procedural, no satellite)
    layer_index_map: np.ndarray        # uint8, terrain-space (row 0 = SOUTH per .ter convention)
    detailed_diffuse_path: Path | None = None  # satellite + per-class PBR detail composite (preview)


def _low_freq_noise(shape: tuple[int, int], scale: float, seed: int) -> np.ndarray:
    """Cheap stand-in for Perlin: blurred Gaussian white noise, normalised to ±1."""
    rng = np.random.default_rng(seed)
    base = rng.standard_normal(shape).astype(np.float32)
    return gaussian_filter(base, sigma=scale) * 6.0  # post-blur amplitude boost


def _build_field_tint(H: int, W: int, *, seed: int = _FIELD_SEED) -> np.ndarray:
    """Voronoi-cell field tints for the patchwork-quilt look.

    Picks `_FIELD_GRID²` jittered cell centres, paints each pixel with the
    nearest centre's small RGB multiplier (within ±15% per channel, biased
    toward green so the overall hue stays believable). Returns float32
    `(H, W, 3)` in roughly the [0.85, 1.18] range.
    """
    rng = np.random.default_rng(seed)
    n = _FIELD_GRID
    # Jittered grid centres in [0, 1]² so cells aren't perfectly aligned
    gx, gy = np.meshgrid(
        (np.arange(n) + 0.5 + rng.uniform(-0.4, 0.4, n)) / n,
        (np.arange(n) + 0.5 + rng.uniform(-0.4, 0.4, n)) / n,
        indexing="xy",
    )
    centres = np.stack([gx.ravel(), gy.ravel()], axis=1)  # (n*n, 2)
    # Per-cell tint — biased green-up, slight red/blue down so adjacent
    # cells alternate between yellow-green and blue-green naturally.
    tints = np.empty((n * n, 3), dtype=np.float32)
    tints[:, 0] = rng.uniform(0.90, 1.04, n * n)  # red
    tints[:, 1] = rng.uniform(0.97, 1.10, n * n)  # green ← biased up
    tints[:, 2] = rng.uniform(0.86, 1.02, n * n)  # blue
    # Per-pixel nearest-cell lookup. Done at low res then upsampled.
    LR = 256
    yy, xx = np.meshgrid(np.linspace(0, 1, LR), np.linspace(0, 1, LR), indexing="ij")
    px = np.stack([xx.ravel(), yy.ravel()], axis=1)  # (LR*LR, 2)
    # Squared distance to each centre, vectorised. n*n=784, LR*LR=65536 →
    # 784*65k = 51M floats = ~200 MB; chunk it to keep memory sane.
    nearest = np.empty(LR * LR, dtype=np.int32)
    chunk = 8192
    for s in range(0, px.shape[0], chunk):
        e = min(s + chunk, px.shape[0])
        d2 = np.sum((px[s:e, None, :] - centres[None, :, :]) ** 2, axis=2)
        nearest[s:e] = np.argmin(d2, axis=1)
    tint_lr = tints[nearest].reshape(LR, LR, 3)
    # Upsample to full resolution with a bit of softness on the boundaries
    # so the cell edges read as gradual hedge-bordered transitions, not
    # hard polygon seams.
    tint_lr = gaussian_filter(tint_lr, sigma=(2.5, 2.5, 0))
    pil = Image.fromarray(np.clip(tint_lr * 255 / 1.5, 0, 255).astype(np.uint8))
    pil = pil.resize((W, H), Image.BILINEAR)
    return np.asarray(pil, dtype=np.float32) * (1.5 / 255.0)


def build_detailed_terrain(
    *,
    layers: list[SplatLayer],
    sat_rgb: np.ndarray,    # (size, size, 3) uint8 — Esri imagery
    out_path: Path,
    tile_count: int = 32,   # how many times each detail texture repeats across the terrain
    detail_blend: float | None = None,  # legacy single-blend; ignored when None
) -> Path:
    """Composite satellite + tiled per-class PBR diffuse, weighted by each
    class's opacity mask. One PNG you can drop on the terrain mesh as a
    single texture — no shader required.

    Per-class detail-blend lifts (see `_DETAIL_BLEND_PER_CLASS`) suppress
    the Esri imagery's brown autumn cast on grass / pasture / forest
    pixels, while leaving river colour mostly to the satellite. Per-field
    Voronoi tints are baked into the grass / pasture / forest pixels so
    adjacent fields read as different greens — the rural-NI patchwork
    look.

    Output is sized to the satellite (typically 2048²)."""
    H, W = sat_rgb.shape[:2]
    detail = np.zeros((H, W, 3), dtype=np.float32)
    total_op = np.zeros((H, W), dtype=np.float32)
    # Effective per-pixel detail-blend strength (weighted by class).
    eff_blend = np.zeros((H, W), dtype=np.float32)
    # Mask of pixels covered by green-class layers (for field tinting).
    green_mask = np.zeros((H, W), dtype=np.float32)

    tile_h = max(1, H // tile_count)
    tile_w = max(1, W // tile_count)
    GRASS_KEYS = {"pasture", "lawn", "forest"}

    for layer in layers:
        if layer.source != "polyhaven":
            continue
        try:
            with Image.open(layer.diffuse_path) as pil:
                pil = pil.convert("RGB").resize((tile_w, tile_h), Image.LANCZOS)
                tile = np.asarray(pil, dtype=np.float32)
        except Exception:
            continue
        ny = (H + tile_h - 1) // tile_h
        nx = (W + tile_w - 1) // tile_w
        tiled = np.tile(tile, (ny, nx, 1))[:H, :W]
        try:
            with Image.open(layer.opacity_path) as op_pil:
                op_pil = op_pil.convert("L").resize((W, H), Image.BILINEAR)
                op = np.asarray(op_pil, dtype=np.float32) / 255.0
        except Exception:
            continue
        detail += tiled * op[..., None]
        total_op += op
        # Per-class blend lift — single-blend caller (legacy) overrides it
        cls_blend = (detail_blend if detail_blend is not None
                     else _DETAIL_BLEND_PER_CLASS.get(layer.cls.key, _DETAIL_BLEND_DEFAULT))
        eff_blend += op * cls_blend
        if layer.cls.key in GRASS_KEYS:
            green_mask += op

    # Where total_op > 0, normalise; elsewhere keep zero
    safe = np.where(total_op > 1e-3, total_op, 1.0)
    detail /= safe[..., None]
    eff_blend /= safe                              # opacity-weighted average
    green_mask = np.clip(green_mask, 0.0, 1.0)

    # ---- per-field tint variation (grass classes only) ----
    if green_mask.max() > 0.05:
        ftint = _build_field_tint(H, W)            # (H, W, 3) ~[0.85, 1.18]
        # Lerp toward the tint by green_mask so non-grass pixels are unaffected
        tint_w = green_mask[..., None]
        detail = detail * (1.0 - tint_w) + (detail * ftint) * tint_w

    sat_f = sat_rgb.astype(np.float32)
    # Per-pixel mix amount. With per-class blends grass pixels barely show
    # the satellite, while river / road pixels still pull colour from it.
    mix_w = np.clip(total_op, 0.0, 1.0) * eff_blend
    out_rgb = sat_f * (1.0 - mix_w[..., None]) + detail * mix_w[..., None]
    out_rgb = np.clip(out_rgb, 0, 255).astype(np.uint8)

    Image.fromarray(out_rgb).save(out_path, optimize=True)
    return out_path


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

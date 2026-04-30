"""Terrain PBR pack — downloads CC0 ground textures from Poly Haven.

One-time download (~120-180 MB at 1k JPG). Each land-cover class gets its
own diffuse + normal + roughness map. Caches in `mapng_ai/cache/pbr/<class>/`
and is picked up by `pipeline.textures.get_ground_texture` automatically.

Poly Haven API is free, CC0, no auth required.
    https://api.polyhaven.com/files/{slug}

Falls back to procedural noise if the download fails for any class.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

import httpx

from mapng_ai import config


log = logging.getLogger(__name__)

POLYHAVEN_API = "https://api.polyhaven.com/files/{slug}"
PBR_CACHE = config.CACHE_DIR / "pbr"


# Mapping from our 8 land-cover classes to Poly Haven asset slugs.
# Picked for visual fit at car-windshield distance + permissive licensing.
# All Poly Haven assets are CC0.
_CLASS_TO_SLUG: dict[str, str | None] = {
    "asphalt":  "asphalt_02",
    "concrete": "concrete_layers_02",
    "lawn":     "leafy_grass",                # lush bright grass for manicured areas
    "pasture":  "leafy_grass",                # lush NI pasture — wet, green, leafy
    "earth":    "mud_forest",
    "gravel":   "gravelly_sand",
    "water":    None,                         # no usable water texture on Poly Haven; procedural is fine
    "forest":   "forest_floor",
}


# Optional secondary slugs per class — when downloaded, the terrain shader
# can blend between primary and secondary based on a low-frequency macro
# noise. Yields large-scale colour/pattern variation across the terrain
# without needing ANOTHER splat layer.
_CLASS_TO_ALT_SLUG: dict[str, str | None] = {
    "pasture":  "sparse_grass",       # drier, thinner — blends naturally with leafy_grass
    "lawn":     "aerial_grass_rock",  # rougher patches in the lawn
    "forest":   "leaves_forest_ground",
    "earth":    "brown_mud_leaves_01",
}


# What we want from each asset: (json key, filename suffix, optional fallback)
_MAPS = [
    ("Diffuse",  "diffuse"),
    ("nor_gl",   "normal"),
    ("Rough",    "roughness"),
]


@dataclass
class TerrainPackProgress:
    total: int
    completed: int
    skipped: int
    failed: int


@dataclass(frozen=True)
class PBRSet:
    diffuse: Path | None
    normal: Path | None
    roughness: Path | None


def pbr_set(class_key: str) -> PBRSet:
    """Return cached PBR maps for a class, with None for any that haven't been
    downloaded — callers fall back to procedural.
    """
    d = PBR_CACHE / class_key
    def _opt(name: str) -> Path | None:
        for ext in (".jpg", ".jpeg", ".png"):
            p = d / f"{name}{ext}"
            if p.exists():
                return p
        return None
    return PBRSet(diffuse=_opt("diffuse"), normal=_opt("normal"), roughness=_opt("roughness"))


def has_real_pbr(class_key: str) -> bool:
    """True iff at least the diffuse map exists for this class."""
    return pbr_set(class_key).diffuse is not None


# ---------------------------------------------------------------------------
async def _download_slug_to(client: httpx.AsyncClient, slug: str, target_dir: Path,
                            *, suffix_prefix: str = "", resolution: str = "1k") -> int:
    """Download diffuse/normal/roughness for one Poly Haven slug into `target_dir`.
    Files saved as `{suffix_prefix}diffuse.{ext}` etc. Returns downloaded count.
    """
    target_dir.mkdir(parents=True, exist_ok=True)
    try:
        r = await client.get(POLYHAVEN_API.format(slug=slug))
        if r.status_code >= 400:
            log.warning("polyhaven %s: %d %s", slug, r.status_code, r.text[:120])
            return 0
        meta = r.json()
    except Exception as exc:
        log.warning("polyhaven %s meta error: %s", slug, exc)
        return 0

    got = 0
    for json_key, suffix in _MAPS:
        if json_key not in meta:
            continue
        res_table = meta[json_key]
        chosen_res = resolution if resolution in res_table else next(iter(res_table))
        formats = res_table[chosen_res]
        url = (formats.get("jpg") or formats.get("png") or {}).get("url")
        if not url:
            continue
        ext = ".jpg" if "jpg" in formats else ".png"
        out_file = target_dir / f"{suffix_prefix}{suffix}{ext}"
        if out_file.exists() and out_file.stat().st_size > 0:
            got += 1
            continue
        try:
            dr = await client.get(url)
            if dr.status_code >= 400:
                continue
            out_file.write_bytes(dr.content)
            got += 1
        except Exception as exc:
            log.warning("polyhaven %s/%s download error: %s", slug, json_key, exc)
    return got


def _composite_with_alt(class_key: str) -> int:
    """If `class_key` has both primary and alt textures cached, FBM-blend them
    in-place so the served `diffuse.jpg` / `normal.jpg` is a natural mix.
    Returns the number of maps composited."""
    import numpy as np
    from PIL import Image
    from scipy.ndimage import gaussian_filter

    d = PBR_CACHE / class_key
    if not d.exists():
        return 0

    rng = np.random.default_rng(hash(class_key) & 0xFFFFFFFF)
    composited = 0
    for kind in ("diffuse", "normal", "roughness"):
        prim = next((d / f"{kind}{ext}" for ext in (".jpg", ".jpeg", ".png")
                     if (d / f"{kind}{ext}").exists()), None)
        alt = next((d / f"alt_{kind}{ext}" for ext in (".jpg", ".jpeg", ".png")
                    if (d / f"alt_{kind}{ext}").exists()), None)
        if prim is None or alt is None:
            continue
        try:
            with Image.open(prim) as a_pil, Image.open(alt) as b_pil:
                size = (max(a_pil.width, 1024), max(a_pil.height, 1024))
                a = np.asarray(a_pil.convert("RGB").resize(size, Image.LANCZOS), dtype=np.float32)
                b = np.asarray(b_pil.convert("RGB").resize(size, Image.LANCZOS), dtype=np.float32)
            # FBM-style mask: smoothed white noise → continuous patches.
            base = rng.standard_normal((size[1], size[0])).astype(np.float32)
            mask = gaussian_filter(base, sigma=size[0] * 0.04)
            mask -= mask.min()
            mask /= max(mask.max(), 1e-6)
            # Strong bias toward primary — the alt should contribute small
            # patches of variation, not dominate the look. ^3 makes most of
            # the mask near zero with rare bumps.
            mask = np.power(mask, 3.0)
            # Cap alt contribution at 35 % to keep the lush primary visible.
            mask = np.clip(mask * 0.35, 0, 1)
            mixed = a * (1 - mask[..., None]) + b * mask[..., None]
            mixed = np.clip(mixed, 0, 255).astype(np.uint8)
            Image.fromarray(mixed).save(prim, optimize=True)
            composited += 1
        except Exception as exc:
            log.warning("composite %s/%s failed: %s", class_key, kind, exc)
    return composited


async def _download_one(client: httpx.AsyncClient, slug: str, class_key: str,
                         resolution: str = "1k", emit=None) -> tuple[int, int]:
    """Download diffuse + normal + roughness for one class, plus an optional
    secondary slug if defined for this class. Composites primary + alt into
    one served file via FBM-blended mask.
    Returns (got, attempted) for the primary slug."""
    out_dir = PBR_CACHE / class_key
    got = await _download_slug_to(client, slug, out_dir, resolution=resolution)
    attempted = len(_MAPS)

    alt_slug = _CLASS_TO_ALT_SLUG.get(class_key)
    composited = 0
    if alt_slug:
        alt_got = await _download_slug_to(client, alt_slug, out_dir,
                                          suffix_prefix="alt_", resolution=resolution)
        if alt_got > 0:
            composited = _composite_with_alt(class_key)

    if emit:
        await emit("class:done", {"class": class_key, "slug": slug,
                                  "downloaded": got, "attempted": attempted,
                                  "alt_slug": alt_slug, "composited": composited})
    return got, attempted


async def download_terrain_pack(*, emit=None) -> TerrainPackProgress:
    """Fetch all classes in parallel."""
    PBR_CACHE.mkdir(parents=True, exist_ok=True)
    progress = TerrainPackProgress(
        total=len(_CLASS_TO_SLUG), completed=0, skipped=0, failed=0,
    )
    if emit:
        await emit("pack:start", {"total": progress.total,
                                  "classes": list(_CLASS_TO_SLUG.keys())})

    async with httpx.AsyncClient(timeout=120.0,
                                 headers={"User-Agent": "mapng-ai/0.1 (research)"}) as client:
        async def _one(class_key: str, slug: str | None):
            if slug is None:
                progress.completed += 1
                progress.skipped += 1
                if emit:
                    await emit("class:skip", {"class": class_key, "slug": "(no PBR; procedural fallback)"})
                return
            if has_real_pbr(class_key):
                progress.completed += 1
                progress.skipped += 1
                if emit:
                    await emit("class:skip", {"class": class_key, "slug": slug})
                return
            got, attempted = await _download_one(client, slug, class_key, emit=emit)
            progress.completed += 1
            if got == 0:
                progress.failed += 1

        await asyncio.gather(*[_one(c, s) for c, s in _CLASS_TO_SLUG.items()])

    if emit:
        await emit("pack:done", {"completed": progress.completed,
                                  "skipped": progress.skipped,
                                  "failed": progress.failed})
    return progress


def pack_status() -> dict:
    """Quick on-disk snapshot for the UI."""
    out = {}
    for class_key, slug in _CLASS_TO_SLUG.items():
        s = pbr_set(class_key)
        out[class_key] = {
            "slug": slug,
            "diffuse": str(s.diffuse.relative_to(config.ROOT)) if s.diffuse else None,
            "normal": str(s.normal.relative_to(config.ROOT)) if s.normal else None,
            "roughness": str(s.roughness.relative_to(config.ROOT)) if s.roughness else None,
        }
    return {
        "classes": out,
        "complete": sum(1 for c in out.values() if c["diffuse"]),
        "total": len(out),
    }

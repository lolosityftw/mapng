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
_CLASS_TO_SLUG: dict[str, str] = {
    "asphalt":  "asphalt_02",
    "concrete": "concrete_layers_02",
    "lawn":     "aerial_grass_rock",          # short well-kept grass
    "pasture":  "aerial_grass_rock",          # NI pasture is similar in look
    "earth":    "mud_forest",
    "gravel":   "gravelly_sand",
    "water":    "water_0011",                 # subtle ripple texture; BeamNG's water shader is the real renderer
    "forest":   "forest_floor_01",
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
async def _download_one(client: httpx.AsyncClient, slug: str, class_key: str,
                         resolution: str = "1k", emit=None) -> tuple[int, int]:
    """Download diffuse + normal + roughness for one class. Returns (got, attempted)."""
    out_dir = PBR_CACHE / class_key
    out_dir.mkdir(parents=True, exist_ok=True)

    # 1) Get the asset's file manifest
    meta_url = POLYHAVEN_API.format(slug=slug)
    try:
        r = await client.get(meta_url)
        if r.status_code >= 400:
            log.warning("polyhaven %s: %d %s", slug, r.status_code, r.text[:120])
            if emit:
                await emit("class:fail", {"class": class_key, "slug": slug,
                                          "reason": f"meta {r.status_code}"})
            return 0, 0
        meta = r.json()
    except Exception as exc:
        log.warning("polyhaven %s meta error: %s", slug, exc)
        if emit:
            await emit("class:fail", {"class": class_key, "slug": slug, "reason": str(exc)})
        return 0, 0

    got = 0
    attempted = 0
    for json_key, suffix in _MAPS:
        if json_key not in meta:
            continue
        # Find the smallest-good resolution + jpg
        res_table = meta[json_key]
        chosen_res = resolution if resolution in res_table else next(iter(res_table))
        formats = res_table[chosen_res]
        url = (formats.get("jpg") or formats.get("png") or {}).get("url")
        if not url:
            continue
        attempted += 1
        ext = ".jpg" if "jpg" in formats else ".png"
        out_file = out_dir / f"{suffix}{ext}"
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

    if emit:
        await emit("class:done", {"class": class_key, "slug": slug,
                                  "downloaded": got, "attempted": attempted})
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
        async def _one(class_key: str, slug: str):
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

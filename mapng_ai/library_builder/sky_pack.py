"""Sky HDRI pack — downloads a single CC0 Poly Haven equirectangular HDRI
for the preview's environment map. The browser swaps the procedural
shader sky for the real photograph, which lifts atmospheric realism more
than any other single addition we've made.

We pick a Northern-European overcast variant by default because it
matches Cookstown's typical grey sky, and we keep the sun direction as
the geometry's own DirectionalLight (no IBL parsing of the HDRI itself).

Cache: `mapng_ai/cache/sky/<slug>.hdr` (a few MB at 1k, ~30 MB at 4k).
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
SKY_CACHE = config.CACHE_DIR / "sky"

# Curated Poly Haven HDRI slugs. Default is the first one — overcast NI.
# If you want a sunny-day fallback for testing flip to one of the others.
_SKY_SLUGS = (
    "kloppenheim_06_pure_sky",   # overcast EU sky, very NI-appropriate
    "qwantani_dusk_2",
    "kiara_1_dawn",
)
DEFAULT_SLUG = _SKY_SLUGS[0]


@dataclass(frozen=True)
class SkyAsset:
    slug: str
    hdr_path: Path
    bytes: int


def cached_path(slug: str = DEFAULT_SLUG) -> Path | None:
    p = SKY_CACHE / f"{slug}.hdr"
    return p if p.exists() and p.stat().st_size > 0 else None


async def _download_one(client: httpx.AsyncClient, slug: str,
                        resolution: str = "1k") -> SkyAsset | None:
    out = SKY_CACHE / f"{slug}.hdr"
    if out.exists() and out.stat().st_size > 0:
        return SkyAsset(slug, out, out.stat().st_size)
    SKY_CACHE.mkdir(parents=True, exist_ok=True)
    try:
        r = await client.get(POLYHAVEN_API.format(slug=slug))
        r.raise_for_status()
        meta = r.json()
    except Exception as exc:
        log.warning("sky meta fetch failed for %s: %s", slug, exc)
        return None
    # Poly Haven HDRIs use the top-level "hdri" map key
    hdri = meta.get("hdri")
    if not isinstance(hdri, dict):
        return None
    res_table = hdri.get(resolution) or next(iter(hdri.values()), {})
    url = (res_table.get("hdr") or {}).get("url")
    if not url:
        return None
    try:
        dr = await client.get(url)
        dr.raise_for_status()
        out.write_bytes(dr.content)
        return SkyAsset(slug, out, len(dr.content))
    except Exception as exc:
        log.warning("sky download failed for %s: %s", slug, exc)
        return None


async def download(slug: str = DEFAULT_SLUG, resolution: str = "1k") -> SkyAsset | None:
    """Async fetch + cache a Poly Haven HDRI. Returns the SkyAsset or None."""
    async with httpx.AsyncClient(timeout=60.0) as client:
        return await _download_one(client, slug, resolution)


def download_sync(slug: str = DEFAULT_SLUG, resolution: str = "1k") -> SkyAsset | None:
    return asyncio.run(download(slug, resolution))


def status() -> dict:
    """For the API: what's cached and what isn't."""
    SKY_CACHE.mkdir(parents=True, exist_ok=True)
    cached = {}
    for slug in _SKY_SLUGS:
        p = SKY_CACHE / f"{slug}.hdr"
        if p.exists() and p.stat().st_size > 0:
            cached[slug] = p.stat().st_size
    return {
        "default_slug": DEFAULT_SLUG,
        "available_slugs": list(_SKY_SLUGS),
        "cached": cached,
    }

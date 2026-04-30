"""Meshy text-to-3D engine — preview + refine for textured GLBs.

Setup (.env at project root):

    MAPNG_API_ENGINE=meshy
    MAPNG_API_KEY=<key from https://www.meshy.ai>
    MAPNG_MESHY_CONCURRENCY=10
    MAPNG_MESHY_RPS=20
    MAPNG_MESHY_TEXTURE=1     # 1 = refine pass (PBR textures), 0 = preview only

Two-stage generation:
    1. POST /v2/text-to-3d  mode=preview          → preview_task_id
       Poll until SUCCEEDED                       → preview GLB ready
    2. POST /v2/text-to-3d  mode=refine,
            preview_task_id=<id>                  → refine_task_id
       Poll until SUCCEEDED                       → textured GLB ready

Refined GLB is what we cache and ship. If the refine pass fails for any
reason, we fall back to the untextured preview GLB so the build doesn't
abort.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from mapng_ai import config
from mapng_ai.assets.base import BuildingAsset


log = logging.getLogger(__name__)


_BASE_URL = "https://api.meshy.ai"
_TEXT_TO_3D = "/v2/text-to-3d"
_CACHE_DIR = config.CACHE_DIR / "meshy"


def _cfg() -> tuple[str, str] | None:
    if os.environ.get("MAPNG_API_ENGINE", "").lower() != "meshy":
        return None
    key = os.environ.get("MAPNG_API_KEY")
    if not key:
        return None
    base = os.environ.get("MAPNG_API_BASE", _BASE_URL).rstrip("/")
    return key, base


def _envf(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def _envb(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() not in ("0", "false", "no", "off", "")


# ---------------------------------------------------------------------------
# Rate limiter — serialised "no more than rps requests per second"
# ---------------------------------------------------------------------------
class _RateLimiter:
    def __init__(self, rate: float) -> None:
        self._min_interval = 1.0 / max(rate, 0.001)
        self._next_allowed = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            wait = max(0.0, self._next_allowed - now)
            self._next_allowed = max(now, self._next_allowed) + self._min_interval
        if wait > 0:
            await asyncio.sleep(wait)


# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------
def _prompt_for_building(building_type: str, levels: int, footprint_m2: float, seed: int) -> str:
    type_word = {
        "residential": "two-storey traditional semi-detached house",
        "house":       "single family detached house",
        "detached":    "detached house",
        "semi":        "semi-detached house",
        "commercial":  "small commercial building",
        "office":      "office building",
        "industrial":  "industrial warehouse with metal roof",
        "warehouse":   "industrial warehouse with corrugated metal roof",
        "retail":      "retail shop with shopfront",
        "shop":        "small shop with shopfront",
        "apartment":   "small apartment block",
        "apartments":  "apartment block",
        "garage":      "small garage with up-and-over door",
        "shed":        "agricultural shed with metal roof",
        "barn":        "stone barn with slate roof",
        "default":     "rural building",
    }.get(building_type, "rural building")
    descriptors = []
    if footprint_m2 < 100: descriptors.append("small")
    elif footprint_m2 > 600: descriptors.append("large")
    if levels >= 3: descriptors.append(f"{levels} storey")
    style_words = ["Northern Ireland rural style", "weathered exterior",
                   "realistic", "exterior only"]
    parts = ["a"] + descriptors + [type_word] + ["in " + ", ".join(style_words)]
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class MeshyAsset:
    fs_path: Path
    rel_path: str


class MeshyEngine:
    def __init__(self,
                 max_concurrency: int | None = None,
                 rate_per_sec: float | None = None,
                 texture: bool | None = None) -> None:
        self.cfg = _cfg()
        self.concurrency = int(max_concurrency or _envf("MAPNG_MESHY_CONCURRENCY", 10))
        self.rps = float(rate_per_sec or _envf("MAPNG_MESHY_RPS", 20))
        self.texture = _envb("MAPNG_MESHY_TEXTURE", True) if texture is None else bool(texture)
        self._sem = asyncio.Semaphore(self.concurrency)
        self._limiter = _RateLimiter(self.rps)
        if self.cfg:
            _CACHE_DIR.mkdir(parents=True, exist_ok=True)

    @property
    def configured(self) -> bool:
        return self.cfg is not None

    def cache_key(self, prompt: str, seed: int, texture: bool, polycount: int | None) -> str:
        # All inputs that change the output go into the cache key. Different
        # polycount targets are separate cached artefacts.
        salt = "tex" if texture else "raw"
        poly = f"p{polycount}" if polycount else "p0"
        return hashlib.sha1(f"{prompt}|{seed}|{salt}|{poly}".encode()).hexdigest()[:20]

    def cached_glb(self, key: str) -> Path:
        return _CACHE_DIR / f"{key}.glb"

    # ---- HTTP helpers ----------------------------------------------------
    async def _request(self, client: httpx.AsyncClient, method: str, url: str,
                       **kwargs) -> httpx.Response:
        await self._limiter.acquire()
        return await client.request(method, url, **kwargs)

    async def _submit_preview(self, client: httpx.AsyncClient, base_url: str,
                              prompt: str, seed: int,
                              target_polycount: int | None = None) -> str | None:
        payload = {
            "mode": "preview",
            "prompt": prompt,
            "art_style": "realistic",
            "negative_prompt": "low quality, low poly, cartoon, text, watermark, ugly",
            "ai_model": "meshy-6",
            "seed": seed,
        }
        if target_polycount is not None and target_polycount > 0:
            payload["target_polycount"] = int(target_polycount)
            payload["should_remesh"] = True
        r = await self._request(client, "POST", f"{base_url}{_TEXT_TO_3D}", json=payload)
        if r.status_code >= 400:
            log.warning("meshy preview submit failed %d: %s", r.status_code, r.text[:200])
            return None
        return r.json().get("result")

    async def _submit_refine(self, client: httpx.AsyncClient, base_url: str,
                             preview_task_id: str) -> str | None:
        payload = {
            "mode": "refine",
            "preview_task_id": preview_task_id,
            "enable_pbr": True,
        }
        r = await self._request(client, "POST", f"{base_url}{_TEXT_TO_3D}", json=payload)
        if r.status_code >= 400:
            log.warning("meshy refine submit failed %d: %s", r.status_code, r.text[:200])
            return None
        return r.json().get("result")

    async def _poll(self, client: httpx.AsyncClient, base_url: str,
                    task_id: str, deadline_s: float = 600) -> dict | None:
        end = time.monotonic() + deadline_s
        while time.monotonic() < end:
            await asyncio.sleep(3)
            r = await self._request(client, "GET", f"{base_url}{_TEXT_TO_3D}/{task_id}")
            if r.status_code >= 400:
                log.warning("meshy poll failed: %s", r.text[:200])
                return None
            info = r.json()
            status = info.get("status")
            if status == "SUCCEEDED":
                return info
            if status in ("FAILED", "CANCELED", "EXPIRED"):
                log.warning("meshy task %s ended in %s", task_id, status)
                return None
        log.warning("meshy task %s exceeded deadline", task_id)
        return None

    # ---- Public API ------------------------------------------------------
    async def generate(self, prompt: str, seed: int,
                       target_polycount: int | None = None) -> Path | None:
        """End-to-end: preview → optional refine → cached GLB path.
        `target_polycount` (if given) is requested directly from Meshy via
        `should_remesh=true` — much cleaner than client-side decimation
        because UV layout stays valid."""
        if not self.cfg:
            return None
        # Apply user's global polycount cap (set on /library; persisted to disk).
        # Env var still wins if explicitly set.
        env_max = int(_envf("MAPNG_MESHY_MAX_POLYCOUNT", 0))
        cap = env_max
        if cap <= 0:
            try:
                from mapng_ai.library_builder.optimise import get_meshy_max_polycount
                cap = get_meshy_max_polycount()
            except Exception:
                cap = 0
        if cap > 0:
            target_polycount = (
                cap if target_polycount is None else min(target_polycount, cap)
            )
        key = self.cache_key(prompt, seed, self.texture, target_polycount)
        out = self.cached_glb(key)
        if out.exists() and out.stat().st_size > 0:
            return out

        api_key, base_url = self.cfg
        headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
        async with self._sem:
            try:
                async with httpx.AsyncClient(timeout=240.0, headers=headers) as client:
                    # 1) Preview
                    preview_id = await self._submit_preview(
                        client, base_url, prompt, seed, target_polycount=target_polycount
                    )
                    if not preview_id:
                        return None
                    preview = await self._poll(client, base_url, preview_id)
                    if not preview:
                        return None

                    # 2) Refine (optional)
                    final = preview
                    if self.texture:
                        refine_id = await self._submit_refine(client, base_url, preview_id)
                        if refine_id:
                            refined = await self._poll(client, base_url, refine_id)
                            if refined:
                                final = refined
                            else:
                                log.warning("meshy refine timed out for %s; using preview", preview_id)

                    glb_url = (final.get("model_urls") or {}).get("glb")
                    if not glb_url:
                        return None
                    dr = await self._request(client, "GET", glb_url)
                    if dr.status_code >= 400:
                        return None
                    out.write_bytes(dr.content)
                    return out
            except (httpx.HTTPError, json.JSONDecodeError) as exc:
                log.warning("meshy network error: %s", exc)
                return None


# ---------------------------------------------------------------------------
async def generate_building_for(
    engine: MeshyEngine,
    *,
    footprint_m2: float,
    levels: int,
    building_type: str,
    seed: int,
    target_polycount: int | None = None,
) -> BuildingAsset | None:
    if not engine.configured:
        return None
    prompt = _prompt_for_building(building_type, levels, footprint_m2, seed)
    glb_path = await engine.generate(prompt, seed, target_polycount=target_polycount)
    if glb_path is None:
        return None
    rel = f"art/shapes/buildings_ai/meshy_{glb_path.stem}.glb"
    side = footprint_m2 ** 0.5
    return BuildingAsset(
        shape_relpath=rel,
        natural_size_m=(side, side, max(3.0, levels * 3.0)),
        color_hex="#cccccc",
        type_label=f"{building_type}_meshy",
    )

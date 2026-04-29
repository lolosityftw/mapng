"""Meshy text-to-3D engine — the first real AI building/asset provider.

Setup:

    set MAPNG_API_ENGINE=meshy
    set MAPNG_API_KEY=<your meshy api key from https://www.meshy.ai>

Or in your shell rc:

    export MAPNG_API_ENGINE=meshy
    export MAPNG_API_KEY=...

How it works:
    - We send a text prompt seeded from the OSM building tags
      (e.g. "two storey red brick semi-detached house with slate roof,
       ground floor windows, exterior")
    - Meshy returns a GLB — we cache it on disk and convert to DAE for BeamNG
    - Subsequent runs hitting the same cache key skip the API call entirely
    - Generation is slow (30-90 s per building), so we cap the count and
      prioritise the largest / most visible buildings; the rest fall back
      to PlaceholderProvider

Cost control:
    MAPNG_MESHY_MAX_BUILDINGS  (default 12)  — hard cap per generation
    MAPNG_MESHY_MIN_AREA_M2    (default 80)  — skip tiny footprints
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
    """Return (api_key, base_url) if env is configured, else None."""
    if os.environ.get("MAPNG_API_ENGINE", "").lower() != "meshy":
        return None
    key = os.environ.get("MAPNG_API_KEY")
    if not key:
        return None
    base = os.environ.get("MAPNG_API_BASE", _BASE_URL).rstrip("/")
    return key, base


# ---------------------------------------------------------------------------
# Prompt builder — OSM tags → text description
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
    if footprint_m2 < 100:
        descriptors.append("small")
    elif footprint_m2 > 600:
        descriptors.append("large")
    if levels >= 3:
        descriptors.append(f"{levels} storey")

    style_words = ["Northern Ireland rural style", "weathered exterior",
                   "realistic", "exterior only"]

    parts = ["a"] + descriptors + [type_word] + ["in " + ", ".join(style_words)]
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class MeshyAsset:
    fs_path: Path        # filesystem location of cached GLB
    rel_path: str        # relative path inside the level zip


class MeshyEngine:
    def __init__(self, max_concurrency: int = 4) -> None:
        self.cfg = _cfg()
        self._sem = asyncio.Semaphore(max_concurrency)
        if self.cfg:
            _CACHE_DIR.mkdir(parents=True, exist_ok=True)

    @property
    def configured(self) -> bool:
        return self.cfg is not None

    def cache_key(self, prompt: str, seed: int) -> str:
        return hashlib.sha1(f"{prompt}|{seed}".encode()).hexdigest()[:20]

    def cached_glb(self, key: str) -> Path:
        return _CACHE_DIR / f"{key}.glb"

    async def generate(self, prompt: str, seed: int) -> Path | None:
        """Return GLB path. Cached if available, else hits the Meshy API.
        Returns None on failure so the caller can fall back."""
        if not self.cfg:
            return None
        key = self.cache_key(prompt, seed)
        out = self.cached_glb(key)
        if out.exists() and out.stat().st_size > 0:
            return out

        api_key, base_url = self.cfg
        headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
        async with self._sem:
            async with httpx.AsyncClient(timeout=240.0, headers=headers) as client:
                # 1) Submit
                payload = {
                    "mode": "preview",
                    "prompt": prompt,
                    "art_style": "realistic",
                    "negative_prompt": "low quality, low poly, cartoon, text, watermark, ugly",
                    "ai_model": "meshy-6",
                    "seed": seed,
                }
                try:
                    r = await client.post(f"{base_url}{_TEXT_TO_3D}", json=payload)
                    if r.status_code >= 400:
                        log.warning("meshy submit failed %d: %s", r.status_code, r.text[:200])
                        return None
                    task_id = r.json().get("result")
                    if not task_id:
                        return None

                    # 2) Poll for completion (preview-mode is usually 30-90s)
                    deadline = time.monotonic() + 600
                    while time.monotonic() < deadline:
                        await asyncio.sleep(5)
                        gr = await client.get(f"{base_url}{_TEXT_TO_3D}/{task_id}")
                        if gr.status_code >= 400:
                            log.warning("meshy poll failed: %s", gr.text[:200])
                            return None
                        info = gr.json()
                        status = info.get("status")
                        if status == "SUCCEEDED":
                            glb_url = info.get("model_urls", {}).get("glb")
                            if not glb_url:
                                return None
                            # 3) Download GLB
                            dr = await client.get(glb_url)
                            if dr.status_code >= 400:
                                return None
                            out.write_bytes(dr.content)
                            return out
                        if status in ("FAILED", "CANCELED", "EXPIRED"):
                            log.warning("meshy task %s ended in %s", task_id, status)
                            return None
                except (httpx.HTTPError, json.JSONDecodeError) as exc:
                    log.warning("meshy network error: %s", exc)
                    return None
        return None


# ---------------------------------------------------------------------------
# Convenience: build a prompt + run the engine for a single building request
# ---------------------------------------------------------------------------
async def generate_building_for(
    engine: MeshyEngine,
    *,
    footprint_m2: float,
    levels: int,
    building_type: str,
    seed: int,
) -> BuildingAsset | None:
    if not engine.configured:
        return None
    prompt = _prompt_for_building(building_type, levels, footprint_m2, seed)
    glb_path = await engine.generate(prompt, seed)
    if glb_path is None:
        return None
    rel = f"art/shapes/buildings_ai/meshy_{glb_path.stem}.glb"
    # Heuristic natural size: assume Meshy returns a unit-ish mesh
    side = footprint_m2 ** 0.5
    return BuildingAsset(
        shape_relpath=rel,
        natural_size_m=(side, side, max(3.0, levels * 3.0)),
        color_hex="#cccccc",
        type_label=f"{building_type}_meshy",
    )

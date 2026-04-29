"""APIProvider — scaffolded slot for AI mesh-generation services (Meshy /
Tripo / Rodin / CSM …).

We don't ship credentials or pick a specific vendor — the user wires that up
in `~/.mapng-ai/api.toml` (or env vars) and selects the engine via config.
Until then this provider returns None for every request, letting the chain
fall through to LibraryProvider / PlaceholderProvider.

Caching: results are stored in `mapng_ai/cache/api/<engine>/<sha1>.glb` so a
single map gen won't re-pay generation costs.
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

from mapng_ai import config
from mapng_ai.assets.base import AssetProvider, BuildingAsset


_CACHE_DIR = config.CACHE_DIR / "api"


@dataclass(frozen=True)
class APIConfig:
    engine: str        # "meshy" | "tripo" | "rodin" | "csm" | …
    api_key: str | None
    base_url: str | None
    timeout_s: float = 120.0


def _config_from_env() -> APIConfig | None:
    engine = os.environ.get("MAPNG_API_ENGINE", "").strip().lower()
    if not engine:
        return None
    return APIConfig(
        engine=engine,
        api_key=os.environ.get("MAPNG_API_KEY"),
        base_url=os.environ.get("MAPNG_API_BASE"),
        timeout_s=float(os.environ.get("MAPNG_API_TIMEOUT_S", "120")),
    )


class APIProvider:
    """Scaffold only — returns None until a real engine adapter is wired up."""
    name = "api"

    def __init__(self, cfg: APIConfig | None = None) -> None:
        self.cfg = cfg or _config_from_env()
        if self.cfg:
            (_CACHE_DIR / self.cfg.engine).mkdir(parents=True, exist_ok=True)

    def can_provide(self, asset_kind: str) -> bool:
        return self.cfg is not None and asset_kind == "building"

    def get_building(
        self,
        footprint_m2: float,
        levels: int,
        building_type: str,
        seed: int,
    ) -> BuildingAsset | None:
        if self.cfg is None:
            return None
        # Stub — call out to self.cfg.engine here. Cache by the (type, levels,
        # rounded footprint, seed) key so deterministic re-runs are free.
        cache_key = hashlib.sha1(
            f"{self.cfg.engine}|{building_type}|{levels}|{int(footprint_m2)}|{seed}".encode()
        ).hexdigest()[:16]
        cached = _CACHE_DIR / self.cfg.engine / f"{cache_key}.glb"
        if cached.exists():
            # If a previous run produced a file for this key, use it
            return BuildingAsset(
                shape_relpath=f"art/shapes/buildings_api/{cache_key}.glb",
                natural_size_m=(footprint_m2 ** 0.5, footprint_m2 ** 0.5, levels * 3.0),
                color_hex="#cccccc",
                type_label=f"{building_type}_api",
            )
        # Real engine call would go here:
        #   r = httpx.post(f"{self.cfg.base_url}/generate", ...)
        #   cached.write_bytes(r.content)
        # For now signal "not provided" so the chain falls through
        return None

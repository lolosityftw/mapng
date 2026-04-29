"""Provider chain: APIProvider → LibraryProvider → PlaceholderProvider.

`get_building` walks the chain and returns the first non-None result. Each
provider's `can_provide` check is honoured; placeholder is the universal
fallback so a building always gets *something*.
"""
from __future__ import annotations

from mapng_ai.assets.api_provider import APIProvider
from mapng_ai.assets.base import BuildingAsset
from mapng_ai.assets.library import LibraryProvider
from mapng_ai.assets.placeholder import PlaceholderProvider


class ProviderChain:
    name = "chain"

    def __init__(self) -> None:
        self.api = APIProvider()
        self.library = LibraryProvider()
        self.placeholder = PlaceholderProvider()

    def can_provide(self, asset_kind: str) -> bool:
        return any(p.can_provide(asset_kind) for p in (self.api, self.library, self.placeholder))

    def get_building(
        self,
        footprint_m2: float,
        levels: int,
        building_type: str,
        seed: int,
    ) -> BuildingAsset:
        for provider in (self.api, self.library, self.placeholder):
            if not provider.can_provide("building"):
                continue
            result = provider.get_building(footprint_m2, levels, building_type, seed)
            if result is not None:
                return result
        # Should never reach here — placeholder always returns something
        return self.placeholder.get_building(footprint_m2, levels, building_type, seed)

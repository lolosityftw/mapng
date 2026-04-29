"""Provider chain: LibraryProvider → PlaceholderProvider.

The runtime pipeline never hits the AI mesh API directly — that's the
batch-builder's job (`python -m mapng_ai.library_builder build`). Once the
library is populated, every map gen is deterministic and free.
"""
from __future__ import annotations

from mapng_ai.assets.base import BuildingAsset
from mapng_ai.assets.library import LibraryProvider
from mapng_ai.assets.placeholder import PlaceholderProvider


class ProviderChain:
    name = "chain"

    def __init__(self) -> None:
        self.library = LibraryProvider()
        self.placeholder = PlaceholderProvider()

    def can_provide(self, asset_kind: str) -> bool:
        return any(p.can_provide(asset_kind) for p in (self.library, self.placeholder))

    def get_building(
        self,
        footprint_m2: float,
        levels: int,
        building_type: str,
        seed: int,
    ) -> BuildingAsset:
        for provider in (self.library, self.placeholder):
            if not provider.can_provide("building"):
                continue
            result = provider.get_building(footprint_m2, levels, building_type, seed)
            if result is not None:
                return result
        return self.placeholder.get_building(footprint_m2, levels, building_type, seed)

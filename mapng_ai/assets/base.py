"""Asset provider protocol — described in docs/SPEC.md §5."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class BuildingAsset:
    """Concrete building model the placement algorithm received from a provider.

    Phase 3 ships only PlaceholderProvider, which returns a unit-cube DAE shared
    by every instance. Future providers (LibraryProvider, APIProvider) will
    return per-instance GLBs/DAEs.
    """
    shape_relpath: str         # path inside the level ZIP, e.g. art/shapes/buildings/box.dae
    natural_size_m: tuple[float, float, float]  # (length, width, height)
    color_hex: str             # used by Three.js preview; BeamNG ignores
    type_label: str            # for diagnostics


class AssetProvider(Protocol):
    name: str

    def get_building(
        self,
        footprint_m2: float,
        levels: int,
        building_type: str,
        seed: int,
    ) -> BuildingAsset: ...

    def can_provide(self, asset_kind: str) -> bool: ...

"""BeamNG asset provider — references shapes from the user's installed
BeamNG levels by path. **Export-only**: the browser preview can't load
DAEs from a game install it doesn't have access to, so the runtime
pipeline still uses LibraryProvider/PlaceholderProvider for the preview
and only swaps in BeamNG references during the BeamNG export step.

Use it by routing the BeamNG zip writer through this provider instead of
LibraryProvider; the preview keeps using LibraryProvider directly.
"""
from __future__ import annotations

import random

from mapng_ai.assets.base import BuildingAsset
from mapng_ai.sources.beamng_assets import BeamNGAsset, cached_scan


# Same alias chain as LibraryProvider so OSM tags match.
_BUILDING_TYPE_ALIASES: dict[str, list[str]] = {
    "house":           ["residential", "default"],
    "detached":        ["residential", "default"],
    "semi":            ["residential", "default"],
    "bungalow":        ["residential", "default"],
    "apartment":       ["residential", "default"],
    "residential":     ["residential", "default"],
    "office":          ["commercial", "default"],
    "shop":            ["shop", "commercial", "default"],
    "retail":          ["shop", "commercial", "default"],
    "commercial":      ["commercial", "default"],
    "church":          ["civic", "default"],
    "school":          ["civic", "default"],
    "civic":           ["civic", "default"],
    "industrial":      ["industrial", "default"],
    "warehouse":       ["industrial", "default"],
    "barn":            ["barn", "shed", "default"],
    "shed":            ["shed", "barn", "default"],
    "farm_auxiliary":  ["shed", "barn", "default"],
    "garage":          ["garage", "default"],
}


class BeamNGAssetProvider:
    name = "beamng"

    def __init__(self) -> None:
        self._index: dict[str, list[BeamNGAsset]] = {"building": [], "tree": [], "vehicle": []}
        for a in cached_scan():
            self._index.setdefault(a.category, []).append(a)
        # Sub-index by type for buildings
        self._by_type: dict[str, list[BeamNGAsset]] = {}
        for a in self._index.get("building", []):
            self._by_type.setdefault(a.type, []).append(a)

    def can_provide(self, asset_kind: str) -> bool:
        if asset_kind == "building":
            return bool(self._index.get("building"))
        if asset_kind == "tree":
            return bool(self._index.get("tree"))
        return False

    def _resolve_type_chain(self, building_type: str) -> list[str]:
        bt = (building_type or "default").lower()
        if bt in _BUILDING_TYPE_ALIASES:
            return _BUILDING_TYPE_ALIASES[bt]
        return [bt, "default"]

    def get_building(self, footprint_m2, levels, building_type, seed) -> BuildingAsset | None:
        # Try alias chain
        for t in self._resolve_type_chain(building_type):
            cands = self._by_type.get(t)
            if cands:
                rng = random.Random(seed)
                pick = rng.choice(cands)
                # Use the asset's ACTUAL natural size from the whitelist —
                # the placement scaler relies on this matching the real DAE
                # bounding box. Override here was the main cause of the
                # 100×-too-big regression.
                return BuildingAsset(
                    shape_relpath=pick.relpath.lstrip("/"),
                    natural_size_m=pick.natural_size_m,
                    color_hex="#999999",
                    type_label=pick.type,
                )
        # Universal fallback: any building shape we have
        all_buildings = self._index.get("building", [])
        if all_buildings:
            rng = random.Random(seed ^ hash(building_type))
            pick = rng.choice(all_buildings)
            return BuildingAsset(
                shape_relpath=pick.relpath.lstrip("/"),
                natural_size_m=pick.natural_size_m,
                color_hex="#999999",
                type_label=pick.type,
            )
        return None

    def pick_tree(self, seed: int) -> BeamNGAsset | None:
        trees = self._index.get("tree", [])
        if not trees:
            return None
        return trees[seed % len(trees)]

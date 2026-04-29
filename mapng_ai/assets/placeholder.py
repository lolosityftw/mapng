"""PlaceholderProvider — what v1 actually ships per spec §5.2.

Generates a single shared unit-cube Collada (DAE) on disk; every building
instance uses it scaled to its OBB. Per-type colours are emitted alongside
each placement for the Three.js preview but BeamNG draws everything in the
shared DAE's material — that is fine for the MVP.
"""
from __future__ import annotations

from pathlib import Path

import trimesh

from mapng_ai.assets.base import AssetProvider, BuildingAsset


# §5.2 palette
_TYPE_COLORS: dict[str, str] = {
    "residential": "#E8D5B7",
    "house":       "#E8D5B7",
    "commercial":  "#7A8DA0",
    "office":      "#7A8DA0",
    "industrial":  "#6B6B6B",
    "warehouse":   "#6B6B6B",
    "retail":      "#C49C7A",
    "shop":        "#C49C7A",
    "apartment":   "#A8957C",
    "apartments":  "#A8957C",
    "garage":      "#8B7355",
    "shed":        "#8B7355",
    "barn":        "#8B7355",
    "default":     "#999999",
}


_BOX_DAE_RELPATH = "art/shapes/buildings/box.dae"


def write_unit_box_dae(target: Path) -> None:
    """Write a 1×1×1 m cube DAE that all placeholder buildings reference."""
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        return
    cube = trimesh.creation.box(extents=(1.0, 1.0, 1.0))
    # Vertex colour the cube so it renders as light grey in BeamNG
    grey = [200, 200, 200, 255]
    cube.visual.vertex_colors = [grey] * len(cube.vertices)
    cube.export(target)


class PlaceholderProvider:
    name = "placeholder"

    def __init__(self) -> None:
        pass

    def can_provide(self, asset_kind: str) -> bool:
        return asset_kind in ("building",)

    def get_building(
        self,
        footprint_m2: float,
        levels: int,
        building_type: str,
        seed: int,
    ) -> BuildingAsset:
        # Heuristic length/width from area, assuming roughly 1.4:1 aspect when
        # we don't know better. Levels × 3 m for height.
        aspect = 1.4
        width = (footprint_m2 / aspect) ** 0.5
        length = width * aspect
        height = max(3.0, levels * 3.0)
        color = _TYPE_COLORS.get(building_type, _TYPE_COLORS["default"])
        return BuildingAsset(
            shape_relpath=_BOX_DAE_RELPATH,
            natural_size_m=(length, width, height),
            color_hex=color,
            type_label=building_type,
        )

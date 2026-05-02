"""PlaceholderProvider — what v1 ships per spec §5.2.

Generates *per-type* unit DAE files (1×1×1 m) with vertex colours baked into
the mesh. This is critical: TSStatic in BeamNG can't easily override colour
per instance, so to get residential/commercial/industrial colour distinctions
in-game we ship a separate DAE per type. Cached on disk so we only build them
once per session.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import trimesh

from mapng_ai import config
from mapng_ai.assets.base import AssetProvider, BuildingAsset


# Spec §5.2 palette
_TYPE_COLORS: dict[str, str] = {
    "residential": "#E8D5B7",
    "house":       "#E8D5B7",
    "detached":    "#E8D5B7",
    "semi":        "#E8D5B7",
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

_ROOF_COLORS: dict[str, str] = {
    "residential": "#7A4F3B",
    "house":       "#7A4F3B",
    "detached":    "#7A4F3B",
    "semi":        "#7A4F3B",
    "commercial":  "#3F4954",
    "office":      "#3F4954",
    "industrial":  "#3F3F3F",
    "warehouse":   "#3F3F3F",
    "retail":      "#5F4636",
    "shop":        "#5F4636",
    "apartment":   "#544738",
    "apartments":  "#544738",
    "garage":      "#4D3F30",
    "shed":        "#4D3F30",
    "barn":        "#4D3F30",
    "default":     "#444444",
}


def _hex_to_rgba(h: str, a: int = 255) -> list[int]:
    h = h.lstrip("#")
    return [int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), a]


_FLAT_ROOF_TYPES: frozenset[str] = frozenset({
    "industrial", "warehouse", "garage", "shed", "barn",
    "commercial", "retail", "shop", "office",
})


# ---------------------------------------------------------------------------
# Pitched-roof box: 1×1×1 unit shape; the BeamNG TSStatic scales per instance
# ---------------------------------------------------------------------------
def _build_pitched_box(wall_rgba: list[int], roof_rgba: list[int],
                        flat_roof: bool = False) -> trimesh.Trimesh:
    box_h = 1.0 if flat_roof else 0.7
    # For flat-roof buildings the "ridge" verts collapse to box-top corners
    ridge_z = box_h if flat_roof else 1.0
    verts = np.array([
        [-0.5, -0.5, 0],          # 0  SW base
        [ 0.5, -0.5, 0],          # 1  SE base
        [ 0.5,  0.5, 0],          # 2  NE base
        [-0.5,  0.5, 0],          # 3  NW base
        [-0.5, -0.5, box_h],      # 4  SW top of box
        [ 0.5, -0.5, box_h],      # 5  SE top
        [ 0.5,  0.5, box_h],      # 6  NE top
        [-0.5,  0.5, box_h],      # 7  NW top
        [-0.5,  0.0, ridge_z],    # 8  W ridge (= box top at midline if flat)
        [ 0.5,  0.0, ridge_z],    # 9  E ridge
    ], dtype=np.float64)

    # Each tuple = (face indices, is_roof?)
    walls = [
        ([0, 2, 1], False), ([0, 3, 2], False),    # base
        ([0, 1, 5], False), ([0, 5, 4], False),    # south wall
        ([1, 2, 6], False), ([1, 6, 5], False),    # east wall
        ([2, 3, 7], False), ([2, 7, 6], False),    # north wall
        ([3, 0, 4], False), ([3, 4, 7], False),    # west wall
        ([4, 8, 7], False), ([4, 5, 9], False),    # west / east gables (split below)
        ([5, 9, 6], False),                         # east gable triangle 1
        ([6, 9, 8], True), ([6, 8, 7], True),       # north roof slope
        ([4, 9, 8], True), ([4, 5, 9], True),       # south roof slope (overrides gable east)
    ]
    # The duplicated [4,5,9] above is a typo from my note-taking — drop it
    # by rebuilding the face list cleanly:
    faces_clean: list[tuple[list[int], bool]] = [
        # base + walls
        ([0, 2, 1], False), ([0, 3, 2], False),
        ([0, 1, 5], False), ([0, 5, 4], False),
        ([1, 2, 6], False), ([1, 6, 5], False),
        ([2, 3, 7], False), ([2, 7, 6], False),
        ([3, 0, 4], False), ([3, 4, 7], False),
        # gables (triangular tops of the box at the ridge ends)
        ([4, 8, 7], False),    # west gable
        ([5, 6, 9], False),    # east gable
        # roof
        ([4, 5, 9], True), ([4, 9, 8], True),  # south slope
        ([7, 9, 6], True), ([7, 8, 9], True),  # north slope
    ]
    faces = np.array([f for f, _ in faces_clean], dtype=np.int64)
    is_roof = np.array([r for _, r in faces_clean], dtype=bool)

    mesh = trimesh.Trimesh(vertices=verts, faces=faces, process=False)
    colours = np.tile(np.array(wall_rgba, dtype=np.uint8), (len(faces), 1))
    colours[is_roof] = roof_rgba
    mesh.visual.face_colors = colours
    return mesh


_PITCHED_DIR_REL = "art/shapes/buildings"


def _pitched_path_for(building_type: str) -> tuple[Path, str]:
    """Return (cache filesystem path, relative path inside the level zip)."""
    key = building_type if building_type in _TYPE_COLORS else "default"
    rel = f"{_PITCHED_DIR_REL}/building_{key}.dae"
    cache_dir = config.CACHE_DIR / "shapes"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / f"building_{key}.dae", rel


def write_pitched_dae(building_type: str) -> tuple[Path, str]:
    """Write the per-type DAE (idempotent). Returns (fs_path, zip_relpath)."""
    cache_path, rel = _pitched_path_for(building_type)
    if cache_path.exists():
        return cache_path, rel
    wall = _hex_to_rgba(_TYPE_COLORS.get(building_type, _TYPE_COLORS["default"]))
    roof = _hex_to_rgba(_ROOF_COLORS.get(building_type, _ROOF_COLORS["default"]))
    flat = building_type in _FLAT_ROOF_TYPES
    mesh = _build_pitched_box(wall, roof, flat_roof=flat)
    mesh.export(cache_path)
    return cache_path, rel


# ---------------------------------------------------------------------------
# Tree: cylinder trunk + cone canopy, 1 m tall (placement scales by height)
# ---------------------------------------------------------------------------
def _build_tree(seed: int) -> trimesh.Trimesh:
    rng = np.random.default_rng(seed)
    trunk_radius = 0.04 + rng.random() * 0.02
    trunk_height = 0.3 + rng.random() * 0.1
    canopy_radius = 0.32 + rng.random() * 0.08
    canopy_height = 1.0 - trunk_height
    trunk = trimesh.creation.cylinder(
        radius=trunk_radius, height=trunk_height, sections=8,
        transform=trimesh.transformations.translation_matrix([0, 0, trunk_height / 2]),
    )
    trunk.visual.face_colors = _hex_to_rgba("#5D4037")
    canopy = trimesh.creation.cone(
        radius=canopy_radius, height=canopy_height, sections=10,
        transform=trimesh.transformations.translation_matrix([0, 0, trunk_height]),
    )
    canopy.visual.face_colors = _hex_to_rgba("#2E7D32")
    return trimesh.util.concatenate([trunk, canopy])


_TREE_REL = "art/shapes/foliage/tree.dae"
_HEDGE_REL = "art/shapes/foliage/hedge.dae"
_WALL_REL = "art/shapes/foliage/wall.dae"
_FENCE_REL = "art/shapes/foliage/fence.dae"
_GATE_REL = "art/shapes/foliage/gate.dae"
_SHED_REL = "art/shapes/buildings/shed.dae"


def write_tree_dae() -> tuple[Path, str]:
    cache_dir = config.CACHE_DIR / "shapes"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / "tree.dae"
    if not cache_path.exists():
        mesh = _build_tree(seed=1)
        mesh.export(cache_path)
    return cache_path, _TREE_REL


def write_hedge_dae() -> tuple[Path, str]:
    """A 1×1×1 unit hedge slab — placement scales X by segment length, Z by height."""
    cache_dir = config.CACHE_DIR / "shapes"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / "hedge.dae"
    if not cache_path.exists():
        # Slightly rounded slab via a box with subdivision and per-vertex Y offset
        slab = trimesh.creation.box(extents=(1.0, 1.0, 1.0))
        slab.apply_translation([0, 0, 0.5])
        # Tint dark hedge green
        slab.visual.face_colors = _hex_to_rgba("#3F5A28")
        slab.export(cache_path)
    return cache_path, _HEDGE_REL


def write_wall_dae() -> tuple[Path, str]:
    """A 1×1×1 unit drystone-wall slab — same scaling convention as the
    hedge but tinted weathered grey so a TSStatic referencing this DAE
    in BeamNG reads as stone, not vegetation."""
    cache_dir = config.CACHE_DIR / "shapes"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / "wall.dae"
    if not cache_path.exists():
        slab = trimesh.creation.box(extents=(1.0, 1.0, 1.0))
        slab.apply_translation([0, 0, 0.5])
        slab.visual.face_colors = _hex_to_rgba("#8A8479")
        slab.export(cache_path)
    return cache_path, _WALL_REL


def write_fence_dae() -> tuple[Path, str]:
    """Thin post-and-rail fence — 1×1×1 unit; placement scales X by length,
    Z by height. Coloured weathered timber so it reads as a fence."""
    cache_dir = config.CACHE_DIR / "shapes"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / "fence.dae"
    if not cache_path.exists():
        slab = trimesh.creation.box(extents=(1.0, 1.0, 1.0))
        slab.apply_translation([0, 0, 0.5])
        slab.visual.face_colors = _hex_to_rgba("#5C4A2C")
        slab.export(cache_path)
    return cache_path, _FENCE_REL


def write_gate_dae() -> tuple[Path, str]:
    """Wooden farm gate — short squat panel sized 4×0.1×1.3 m. Placed at
    the spot where a hedge meets a road."""
    cache_dir = config.CACHE_DIR / "shapes"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / "gate.dae"
    if not cache_path.exists():
        slab = trimesh.creation.box(extents=(1.0, 1.0, 1.0))
        slab.apply_translation([0, 0, 0.5])
        slab.visual.face_colors = _hex_to_rgba("#6E5530")
        slab.export(cache_path)
    return cache_path, _GATE_REL


def write_shed_dae() -> tuple[Path, str]:
    """Small farmyard shed — flat-roofed 1×1×1 unit, corrugated grey."""
    cache_dir = config.CACHE_DIR / "shapes"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / "shed.dae"
    if not cache_path.exists():
        slab = trimesh.creation.box(extents=(1.0, 1.0, 1.0))
        slab.apply_translation([0, 0, 0.5])
        slab.visual.face_colors = _hex_to_rgba("#7A7A75")
        slab.export(cache_path)
    return cache_path, _SHED_REL


# ---------------------------------------------------------------------------
class PlaceholderProvider:
    name = "placeholder"

    def can_provide(self, asset_kind: str) -> bool:
        return asset_kind in ("building", "tree", "hedge")

    def get_building(
        self,
        footprint_m2: float,
        levels: int,
        building_type: str,
        seed: int,
    ) -> BuildingAsset:
        cache_path, rel = write_pitched_dae(building_type)
        aspect = 1.4
        width = (footprint_m2 / aspect) ** 0.5
        length = width * aspect
        height = max(3.0, levels * 3.0)
        color = _TYPE_COLORS.get(building_type, _TYPE_COLORS["default"])
        return BuildingAsset(
            shape_relpath=rel,
            natural_size_m=(length, width, height),
            color_hex=color,
            type_label=building_type,
        )

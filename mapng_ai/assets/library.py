"""LibraryProvider — read user-supplied building meshes.

Drop CC0 / CC-BY building meshes (DAE or GLB) into:

    assets/buildings/<type>/

…where `<type>` matches one of the OSM building types
(`residential`, `commercial`, `industrial`, `retail`, `apartment`,
`garage`, `default`, …). Each folder may contain a `manifest.json` with
overrides:

    {
      "model_a.dae": { "footprint_m": [12, 8], "levels": 2 },
      ...
    }

If no manifest is present, footprint and levels are guessed from the mesh's
axis-aligned bounding box (X×Y for footprint, Z/3 for floors).

Falls back to PlaceholderProvider for any (type, size) it can't satisfy.
"""
from __future__ import annotations

import json
import random
from dataclasses import dataclass
from pathlib import Path

import trimesh

from mapng_ai import config
from mapng_ai.assets.base import AssetProvider, BuildingAsset


_LIBRARY_ROOT = config.ROOT / "assets" / "buildings"


@dataclass(frozen=True)
class _LibraryEntry:
    rel_path: str           # relative to library root, used inside the level zip
    fs_path: Path
    footprint_m: tuple[float, float]
    levels: int
    type_label: str


def _scan_library() -> dict[str, list[_LibraryEntry]]:
    """Walk LIBRARY_ROOT once and return entries grouped by type."""
    result: dict[str, list[_LibraryEntry]] = {}
    if not _LIBRARY_ROOT.exists():
        return result
    for type_dir in sorted(_LIBRARY_ROOT.iterdir()):
        if not type_dir.is_dir():
            continue
        type_label = type_dir.name.lower()
        manifest_path = type_dir / "manifest.json"
        manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
        for mesh_path in sorted(type_dir.iterdir()):
            if mesh_path.suffix.lower() not in (".dae", ".glb", ".gltf"):
                continue
            override = manifest.get(mesh_path.name, {})
            footprint = override.get("footprint_m")
            levels = override.get("levels")
            if footprint is None or levels is None:
                try:
                    mesh = trimesh.load(mesh_path)
                    if hasattr(mesh, "extents"):
                        ex, ey, ez = mesh.extents
                        if footprint is None:
                            footprint = [float(ex), float(ey)]
                        if levels is None:
                            levels = max(1, int(round(float(ez) / 3.0)))
                except Exception:
                    continue
            entry = _LibraryEntry(
                rel_path=f"art/shapes/buildings_lib/{type_label}/{mesh_path.name}",
                fs_path=mesh_path,
                footprint_m=tuple(footprint),
                levels=int(levels),
                type_label=type_label,
            )
            result.setdefault(type_label, []).append(entry)
    return result


class LibraryProvider:
    name = "library"

    def __init__(self) -> None:
        self._index = _scan_library()

    def can_provide(self, asset_kind: str) -> bool:
        return asset_kind == "building" and bool(self._index)

    def get_building(
        self,
        footprint_m2: float,
        levels: int,
        building_type: str,
        seed: int,
    ) -> BuildingAsset | None:
        candidates = self._index.get(building_type) or self._index.get("default")
        if not candidates:
            return None
        # Pick deterministically by seed, biased toward similar floor count
        candidates_sorted = sorted(candidates, key=lambda e: abs(e.levels - levels))
        rng = random.Random(seed)
        # Top 3 by closest level → uniform random one of those
        pool = candidates_sorted[: min(3, len(candidates_sorted))]
        chosen = rng.choice(pool)
        l, w = chosen.footprint_m
        return BuildingAsset(
            shape_relpath=chosen.rel_path,
            natural_size_m=(float(l), float(w), max(3.0, chosen.levels * 3.0)),
            color_hex="#999999",
            type_label=chosen.type_label,
        )


# ---------------------------------------------------------------------------
# Tree library — same idea, but for trees/<species>/ folders
# ---------------------------------------------------------------------------
_TREE_ROOT = config.ROOT / "assets" / "trees"
_VEHICLE_ROOT = config.ROOT / "assets" / "vehicles"


@dataclass(frozen=True)
class _LeafEntry:
    rel_path: str
    fs_path: Path
    type_label: str


def _scan_simple(root: Path, kind: str) -> dict[str, list[_LeafEntry]]:
    out: dict[str, list[_LeafEntry]] = {}
    if not root.exists():
        return out
    for sub in sorted(root.iterdir()):
        if not sub.is_dir():
            continue
        for mesh_path in sorted(sub.iterdir()):
            if mesh_path.suffix.lower() not in (".dae", ".glb", ".gltf"):
                continue
            entry = _LeafEntry(
                rel_path=f"art/shapes/{kind}_lib/{sub.name}/{mesh_path.name}",
                fs_path=mesh_path,
                type_label=sub.name.lower(),
            )
            out.setdefault(sub.name.lower(), []).append(entry)
    return out


_TREE_INDEX: dict[str, list[_LeafEntry]] | None = None
_VEHICLE_INDEX: dict[str, list[_LeafEntry]] | None = None


def tree_library() -> dict[str, list[_LeafEntry]]:
    global _TREE_INDEX
    if _TREE_INDEX is None:
        _TREE_INDEX = _scan_simple(_TREE_ROOT, "trees")
    return _TREE_INDEX


def vehicle_library() -> dict[str, list[_LeafEntry]]:
    global _VEHICLE_INDEX
    if _VEHICLE_INDEX is None:
        _VEHICLE_INDEX = _scan_simple(_VEHICLE_ROOT, "vehicles")
    return _VEHICLE_INDEX


def pick_tree(seed: int) -> _LeafEntry | None:
    """Return a deterministic tree from the library, None if empty."""
    idx = tree_library()
    flat = [e for entries in idx.values() for e in entries]
    if not flat:
        return None
    return flat[seed % len(flat)]


def pick_vehicle(kind: str | None, seed: int) -> _LeafEntry | None:
    idx = vehicle_library()
    if kind and kind in idx:
        candidates = idx[kind]
    else:
        candidates = [e for entries in idx.values() for e in entries]
    if not candidates:
        return None
    return candidates[seed % len(candidates)]

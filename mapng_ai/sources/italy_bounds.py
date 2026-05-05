"""Measure actual bounding boxes of Italy DAE assets via trimesh.

The previous "hand-eyeball the size" approach in beamng_assets.py was
wildly off — palazzo measured 56m but I'd said 20m, apartment was 32m
tall but I'd said 15m. That's where the "buildings 100× too big" came
from: scale = OSM_footprint / wrong_estimate, applied to the actual
mesh, gives garbage.

This module measures real bounds at first run and caches the results
to a JSON file alongside the source. Cache lives at
`mapng_ai/sources/italy_bounds.json` so repeat runs don't re-parse DAEs.
"""
from __future__ import annotations

import json
import zipfile
import tempfile
import os
from pathlib import Path
from typing import Dict, Tuple


_CACHE_PATH = Path(__file__).parent / "italy_bounds.json"


def _italy_zip_path() -> Path | None:
    """Find Italy.zip on the user's machine."""
    candidates = [
        Path("D:/SteamLibrary/steamapps/common/BeamNG.drive/content/levels/italy.zip"),
        Path("C:/Program Files (x86)/Steam/steamapps/common/BeamNG.drive/content/levels/italy.zip"),
        Path("C:/Program Files/Steam/steamapps/common/BeamNG.drive/content/levels/italy.zip"),
        Path("E:/SteamLibrary/steamapps/common/BeamNG.drive/content/levels/italy.zip"),
    ]
    for p in candidates:
        if p.is_file():
            return p
    return None


def _measure_dae(zf: zipfile.ZipFile, member: str) -> Tuple[float, float, float] | None:
    """Load a DAE from a zip member and return (length, width, height)."""
    try:
        import trimesh
    except ImportError:
        return None
    try:
        data = zf.read(member)
    except KeyError:
        return None
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, os.path.basename(member))
        with open(path, "wb") as f:
            f.write(data)
        try:
            mesh = trimesh.load(path, force="mesh")
            if not hasattr(mesh, "bounds") or mesh.bounds is None:
                return None
            size = mesh.bounds[1] - mesh.bounds[0]
            return (float(size[0]), float(size[1]), float(size[2]))
        except Exception:
            return None


def load_cache() -> Dict[str, Tuple[float, float, float]]:
    """Read the bounds cache (relpath → (l, w, h)). Empty if cache missing."""
    if not _CACHE_PATH.is_file():
        return {}
    try:
        raw = json.loads(_CACHE_PATH.read_text())
        return {k: tuple(v) for k, v in raw.items()}
    except (json.JSONDecodeError, OSError):
        return {}


def get_or_measure(relpaths: list[str]) -> Dict[str, Tuple[float, float, float]]:
    """Return measured bounds for the given asset relpaths.

    Uses cached values when present; measures + caches anything missing.
    Falls back to (10, 8, 5) if Italy.zip isn't available or trimesh fails.
    """
    cache = load_cache()
    needed = [p for p in relpaths if p not in cache]
    if not needed:
        return {p: cache[p] for p in relpaths}

    italy = _italy_zip_path()
    if italy is None:
        # No Italy install; return cache + safe defaults
        return {p: cache.get(p, (10.0, 8.0, 5.0)) for p in relpaths}

    try:
        with zipfile.ZipFile(italy) as zf:
            for relpath in needed:
                # relpath starts with /levels/italy/... — strip the leading /
                member = relpath.lstrip("/")
                bounds = _measure_dae(zf, member)
                if bounds is not None:
                    cache[relpath] = bounds
    except (zipfile.BadZipFile, OSError):
        pass

    # Persist
    try:
        _CACHE_PATH.write_text(json.dumps(
            {k: list(v) for k, v in cache.items()}, indent=2, sort_keys=True
        ))
    except OSError:
        pass

    return {p: cache.get(p, (10.0, 8.0, 5.0)) for p in relpaths}


if __name__ == "__main__":
    # Run manually to populate the cache for the curated Italy whitelist
    PATHS = [
        "/levels/italy/art/shapes/buildings/italy_town_bld1.dae",
        "/levels/italy/art/shapes/buildings/italy_town_bld2.dae",
        "/levels/italy/art/shapes/buildings/italy_town_bld3.dae",
        "/levels/italy/art/shapes/buildings/italy_town_bld4.dae",
        "/levels/italy/art/shapes/buildings/italy_town_bld5.dae",
        "/levels/italy/art/shapes/buildings/italy_town_bld6.dae",
        "/levels/italy/art/shapes/buildings/italy_town_bld7.dae",
        "/levels/italy/art/shapes/buildings/italy_town_bld8.dae",
        "/levels/italy/art/shapes/buildings/italy_town_bld9.dae",
        "/levels/italy/art/shapes/buildings/italy_town_bld10.dae",
        "/levels/italy/art/shapes/buildings/italy_town_bld11.dae",
        "/levels/italy/art/shapes/buildings/italy_town_bld12.dae",
        "/levels/italy/art/shapes/buildings/italy_town_bld13.dae",
        "/levels/italy/art/shapes/buildings/italy_town_bld14.dae",
        "/levels/italy/art/shapes/buildings/italy_town_bld15.dae",
        "/levels/italy/art/shapes/buildings/italy_town_bld16.dae",
        "/levels/italy/art/shapes/buildings/italy_bld_20x12_apartment.dae",
        "/levels/italy/art/shapes/buildings/italy_bld_small_church.dae",
        "/levels/italy/art/shapes/buildings/italy_bld_church_village.dae",
        "/levels/italy/art/shapes/trees/trees_italy/holm_oak.dae",
        "/levels/italy/art/shapes/trees/trees_italy/holm_oak_city_small.dae",
        "/levels/italy/art/shapes/trees/trees_italy/holm_oak_city_tall.dae",
        "/levels/italy/art/shapes/trees/trees_italy/cypress_tree.dae",
        "/levels/italy/art/shapes/trees/trees_italy/maritime_pine.dae",
        "/levels/italy/art/shapes/trees/trees_italy/maritime_pine_2.dae",
        "/levels/italy/art/shapes/trees/trees_italy/scots_pine.dae",
        "/levels/italy/art/shapes/trees/trees_italy/olive.dae",
        "/levels/italy/art/shapes/trees/trees_italy/cork_oak_medium.dae",
        "/levels/italy/art/shapes/trees/trees_italy/cork_oak_large_1.dae",
        "/levels/italy/art/shapes/trees/trees_italy/generibush.dae",
        "/levels/italy/art/shapes/trees/trees_italy/fluffy_bush.dae",
    ]
    bounds = get_or_measure(PATHS)
    for p in PATHS:
        l, w, h = bounds[p]
        print(f"  {p.rsplit('/', 1)[-1]:<40} {l:6.2f} x {w:6.2f} x {h:6.2f} m")

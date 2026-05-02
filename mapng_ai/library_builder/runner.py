"""Library helpers — file layout + status, no generation.

Asset acquisition is now a manual / drag-drop flow (see pack_import.py
for zip imports). This module just exposes the on-disk layout
conventions so the rest of the codebase can resolve where each
catalogue entry's GLB *would* live.
"""
from __future__ import annotations

from pathlib import Path

from mapng_ai import config
from mapng_ai.library_builder.catalogue import CATALOGUE, CatalogueEntry


_BUILDINGS_DIR = config.ROOT / "assets" / "buildings"
_TREES_DIR = config.ROOT / "assets" / "trees"
_VEHICLES_DIR = config.ROOT / "assets" / "vehicles"


def target_dir(entry: CatalogueEntry) -> Path:
    base = {"building": _BUILDINGS_DIR, "tree": _TREES_DIR, "vehicle": _VEHICLES_DIR}[entry.category]
    return base / entry.type


def target_glb(entry: CatalogueEntry) -> Path:
    return target_dir(entry) / f"{entry.slug}.glb"


def manifest_path(entry: CatalogueEntry) -> Path:
    return target_dir(entry) / "manifest.json"


def library_status() -> dict:
    """Quick on-disk snapshot for the UI."""
    by_cat: dict[str, dict[str, int]] = {"building": {}, "tree": {}, "vehicle": {}}
    for e in CATALOGUE:
        glb = target_glb(e)
        by_cat[e.category].setdefault(e.type, 0)
        if glb.exists() and glb.stat().st_size > 0:
            by_cat[e.category][e.type] += 1
    totals = {cat: sum(types.values()) for cat, types in by_cat.items()}
    return {
        "totals": totals,
        "by_category": by_cat,
        "catalogue_size": len(CATALOGUE),
    }

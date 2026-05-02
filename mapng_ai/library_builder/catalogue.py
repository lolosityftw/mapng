"""Region asset catalogue — declarative type registry.

Without an AI generator, the catalogue is no longer a 'schedule of things
to make'. It's now a **wish list** the Asset Browser uses to organise
imports: one entry per (category, type, slug) the rural-NI pack would
ideally include.

Filling the library happens through:
  1. Drag-drop GLB onto an Asset Browser entry  (per-slug replacement)
  2. Pack import: drop a zip from Quaternius / Kenney  (auto-categorised)
  3. BeamNG asset reference mode (export-only — see sources/beamng_assets.py)

Slug filenames are still used as the canonical name on disk
(`assets/buildings/<type>/<slug>.glb`) so prompts/aliases keep working;
they're optional descriptors only.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from mapng_ai import config


@dataclass(frozen=True)
class CatalogueEntry:
    category: str           # "building" | "tree" | "vehicle"
    type: str               # bucket directory under assets/<category>/
    slug: str               # filename-safe id, used as <slug>.glb on disk
    description: str        # human-readable hint for what this is
    footprint_m: tuple[float, float] = (10.0, 8.0)
    levels: int = 1


# ---------------------------------------------------------------------------
# 7 buildings — the most common rural-NI types
# ---------------------------------------------------------------------------
BUILDINGS: list[CatalogueEntry] = [
    CatalogueEntry("building", "residential", "ni_semi_detached",
                   "Two-storey pebbledash semi-detached house with slate roof",
                   footprint_m=(12, 8), levels=2),
    CatalogueEntry("building", "residential", "ni_bungalow",
                   "Single storey country bungalow with hipped slate roof",
                   footprint_m=(14, 9), levels=1),
    CatalogueEntry("building", "residential", "ni_cottage",
                   "Whitewashed traditional Irish cottage with slate roof",
                   footprint_m=(10, 6), levels=1),
    CatalogueEntry("building", "commercial", "ni_country_pub",
                   "Small country pub or inn with hanging sign",
                   footprint_m=(14, 10), levels=2),
    CatalogueEntry("building", "civic", "ni_parish_church",
                   "Small parish church with stone walls and modest belltower",
                   footprint_m=(20, 10), levels=2),
    CatalogueEntry("building", "civic", "ni_school",
                   "Small rural primary school, single storey red brick",
                   footprint_m=(25, 12), levels=1),
    CatalogueEntry("building", "barn", "ni_stone_barn",
                   "Traditional stone barn with corrugated metal roof",
                   footprint_m=(12, 7), levels=1),
]


# ---------------------------------------------------------------------------
# 3 trees
# ---------------------------------------------------------------------------
TREES: list[CatalogueEntry] = [
    CatalogueEntry("tree", "oak", "tree_oak",
                   "Mature oak with broad leafy canopy",
                   footprint_m=(8, 8), levels=1),
    CatalogueEntry("tree", "spruce", "tree_sitka_spruce",
                   "Tall sitka spruce conifer with conical canopy",
                   footprint_m=(5, 5), levels=1),
    CatalogueEntry("tree", "hawthorn", "tree_hawthorn",
                   "Small gnarled hawthorn for hedgerows + field boundaries",
                   footprint_m=(4, 4), levels=1),
]


VEHICLES: list[CatalogueEntry] = []   # deferred for now


CATALOGUE: list[CatalogueEntry] = BUILDINGS + TREES + VEHICLES


def by_category(category: str) -> list[CatalogueEntry]:
    return [e for e in CATALOGUE if e.category == category]


def total_count() -> int:
    return len(CATALOGUE)


# ---------------------------------------------------------------------------
# Per-slug prompt overrides (kept for backward compat — unused without an
# AI generator, but the Asset Browser still surfaces them as 'description
# overrides' in case future generation paths return)
# ---------------------------------------------------------------------------
def _overrides_path() -> Path:
    return config.CACHE_DIR / "prompt_overrides.json"


def get_prompt_override(slug: str) -> str | None:
    p = _overrides_path()
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8")).get(slug)
    except Exception:
        return None


def set_prompt_override(slug: str, prompt: str | None) -> None:
    p = _overrides_path()
    data: dict[str, str] = {}
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    if prompt:
        data[slug] = prompt
    else:
        data.pop(slug, None)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


def effective_prompt(entry: CatalogueEntry) -> str:
    return get_prompt_override(entry.slug) or entry.description

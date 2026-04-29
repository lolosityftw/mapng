"""Region-specific asset catalogue — what the batch builder generates.

Generated once via Meshy (~$5-10 of credits depending on tier), then reused
on every map gen via LibraryProvider. Cached on disk; rebuilds skip already-
present entries.

Conventions:
- `slug` is filesystem-safe and used both for the GLB filename and the asset
  manifest key.
- `prompt` is what we send to Meshy.
- Buildings have a `type` matching OSM `building=*` tags so LibraryProvider
  picks them up automatically.
- `footprint_m` and `levels` are baked into the manifest so the placement
  algorithm doesn't need to inspect the mesh.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CatalogueEntry:
    category: str           # "building" | "tree" | "vehicle"
    type: str               # OSM/asset type bucket
    slug: str               # filename-safe id, unique across the catalogue
    prompt: str             # text passed to Meshy
    footprint_m: tuple[float, float] = (10.0, 8.0)
    levels: int = 1
    art_style: str = "realistic"


_NI_BG = "Northern Ireland rural style, weathered exterior, realistic, exterior only, daylight"


# ---------------------------------------------------------------------------
# 24 building variations — covers what you'll see in any rural NI bbox
# ---------------------------------------------------------------------------
BUILDINGS: list[CatalogueEntry] = [
    # Residential — 8
    CatalogueEntry("building", "residential", "ni_semi_detached_a",
                   f"a two storey semi-detached pebbledash house with slate roof, 1970s, {_NI_BG}",
                   footprint_m=(12, 8), levels=2),
    CatalogueEntry("building", "residential", "ni_semi_detached_b",
                   f"a two storey semi-detached red brick house with grey tile roof, 1990s, {_NI_BG}",
                   footprint_m=(12, 8), levels=2),
    CatalogueEntry("building", "residential", "ni_detached_modern",
                   f"a single family modern detached two storey home with porch, render walls, slate roof, {_NI_BG}",
                   footprint_m=(15, 10), levels=2),
    CatalogueEntry("building", "residential", "ni_bungalow",
                   f"a single storey country bungalow with hipped slate roof and chimney, {_NI_BG}",
                   footprint_m=(14, 9), levels=1),
    CatalogueEntry("building", "residential", "ni_cottage_traditional",
                   f"a small traditional whitewashed Irish cottage with thatched or slate roof, {_NI_BG}",
                   footprint_m=(10, 6), levels=1),
    CatalogueEntry("building", "residential", "ni_terrace_townhouse",
                   f"a two storey end terrace townhouse with slate roof and small front yard, {_NI_BG}",
                   footprint_m=(7, 9), levels=2),
    CatalogueEntry("building", "residential", "ni_dormer_bungalow",
                   f"a dormer bungalow with two front-facing dormer windows, {_NI_BG}",
                   footprint_m=(13, 9), levels=2),
    CatalogueEntry("building", "residential", "ni_modest_house",
                   f"a modest two storey rural house with simple pitched roof and back kitchen extension, {_NI_BG}",
                   footprint_m=(11, 8), levels=2),

    # Commercial / civic — 6
    CatalogueEntry("building", "commercial", "ni_country_pub",
                   f"a small country pub or inn, painted exterior, hanging sign, slate roof, {_NI_BG}",
                   footprint_m=(14, 10), levels=2),
    CatalogueEntry("building", "shop", "ni_village_shop",
                   f"a single storey village shop with shopfront windows and signage, {_NI_BG}",
                   footprint_m=(10, 8), levels=1),
    CatalogueEntry("building", "commercial", "ni_petrol_station",
                   f"a small rural petrol station with single forecourt and convenience shop, {_NI_BG}",
                   footprint_m=(20, 12), levels=1),
    CatalogueEntry("building", "civic", "ni_parish_church",
                   f"a small parish church, stone walls, slate roof, modest belltower, {_NI_BG}",
                   footprint_m=(20, 10), levels=2),
    CatalogueEntry("building", "civic", "ni_school",
                   f"a small rural primary school, single storey red brick, large windows, {_NI_BG}",
                   footprint_m=(25, 12), levels=1),
    CatalogueEntry("building", "civic", "ni_hall",
                   f"a small community hall or scout hall, single storey, corrugated metal roof, {_NI_BG}",
                   footprint_m=(15, 10), levels=1),

    # Agricultural / industrial — 8
    CatalogueEntry("building", "barn", "ni_stone_barn",
                   f"a traditional stone barn with corrugated metal roof, weathered, hay door, {_NI_BG}",
                   footprint_m=(12, 7), levels=1),
    CatalogueEntry("building", "barn", "ni_dairy_parlour",
                   f"a single storey concrete-block dairy parlour with corrugated metal roof and milk tank, {_NI_BG}",
                   footprint_m=(20, 12), levels=1),
    CatalogueEntry("building", "shed", "ni_machinery_shed",
                   f"an open-fronted three-bay machinery shed with corrugated metal roof, {_NI_BG}",
                   footprint_m=(18, 9), levels=1),
    CatalogueEntry("building", "industrial", "ni_poultry_shed",
                   f"a long low poultry broiler shed with white walls and corrugated metal roof, ventilation fans, {_NI_BG}",
                   footprint_m=(60, 15), levels=1),
    CatalogueEntry("building", "industrial", "ni_grain_silo_pair",
                   f"two large grain silos beside a small concrete farm building, {_NI_BG}",
                   footprint_m=(10, 10), levels=3),
    CatalogueEntry("building", "shed", "ni_cattle_shed",
                   f"a cattle shed with slatted concrete walls and corrugated metal roof, {_NI_BG}",
                   footprint_m=(25, 14), levels=1),
    CatalogueEntry("building", "garage", "ni_double_garage",
                   f"a small detached double garage with two roller doors, {_NI_BG}",
                   footprint_m=(8, 6), levels=1),
    CatalogueEntry("building", "industrial", "ni_warehouse",
                   f"a small rural distribution warehouse, corrugated metal walls and roof, loading bay, {_NI_BG}",
                   footprint_m=(35, 20), levels=1),

    # Misc — 2
    CatalogueEntry("building", "default", "ni_outbuilding_small",
                   f"a small generic outbuilding, breeze block walls, shed roof, {_NI_BG}",
                   footprint_m=(5, 4), levels=1),
    CatalogueEntry("building", "default", "ni_storage_container",
                   f"a 20-foot shipping container in a farmyard, weathered corrugated steel, {_NI_BG}",
                   footprint_m=(6, 2.4), levels=1),
]


# ---------------------------------------------------------------------------
# Trees — 6 species typical of NI hedgerow + plantation
# ---------------------------------------------------------------------------
TREES: list[CatalogueEntry] = [
    CatalogueEntry("tree", "oak", "tree_oak",
                   "a single mature oak tree, full leaf, isolated, on grass, realistic, daylight",
                   footprint_m=(8, 8), levels=1),
    CatalogueEntry("tree", "ash", "tree_ash",
                   "a single mature ash tree, full leaf, isolated, on grass, realistic, daylight",
                   footprint_m=(7, 7), levels=1),
    CatalogueEntry("tree", "beech", "tree_beech",
                   "a single mature beech tree, dense canopy, on grass, realistic, daylight",
                   footprint_m=(9, 9), levels=1),
    CatalogueEntry("tree", "spruce", "tree_sitka_spruce",
                   "a tall sitka spruce conifer, isolated, plantation style, realistic, daylight",
                   footprint_m=(5, 5), levels=1),
    CatalogueEntry("tree", "hawthorn", "tree_hawthorn",
                   "a small hawthorn tree on a field boundary, gnarled trunk, sparse canopy, realistic, daylight",
                   footprint_m=(4, 4), levels=1),
    CatalogueEntry("tree", "sycamore", "tree_sycamore",
                   "a sycamore tree with broad leafy canopy, isolated, on grass, realistic, daylight",
                   footprint_m=(8, 8), levels=1),
]


# ---------------------------------------------------------------------------
# Vehicles — placeable static decorations near farms / villages
# ---------------------------------------------------------------------------
VEHICLES: list[CatalogueEntry] = [
    CatalogueEntry("vehicle", "tractor", "vehicle_tractor_old",
                   "a small old red farm tractor, weathered, parked, realistic, daylight",
                   footprint_m=(4, 2), levels=1),
    CatalogueEntry("vehicle", "bus", "vehicle_school_bus",
                   "a Northern Ireland Translink school bus, white and blue, parked, realistic, daylight",
                   footprint_m=(11, 2.5), levels=1),
    CatalogueEntry("vehicle", "lorry", "vehicle_old_lorry",
                   "an old white box lorry, parked, weathered, realistic, daylight",
                   footprint_m=(8, 2.5), levels=1),
    CatalogueEntry("vehicle", "trailer", "vehicle_farm_trailer",
                   "an empty agricultural trailer, parked in farmyard, realistic, daylight",
                   footprint_m=(5, 2), levels=1),
]


CATALOGUE: list[CatalogueEntry] = BUILDINGS + TREES + VEHICLES


def by_category(category: str) -> list[CatalogueEntry]:
    return [e for e in CATALOGUE if e.category == category]


def total_count() -> int:
    return len(CATALOGUE)

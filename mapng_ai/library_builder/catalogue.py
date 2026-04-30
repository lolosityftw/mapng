"""Region asset catalogue — 12-entry trim focused on rural NI.

We now ask Meshy for *small* meshes from the start (`target_polycount` on
preview) instead of generating high-poly and decimating client-side. This
keeps UV layouts intact (no seam welds) so even at low resolutions the
textures don't break.

Each entry declares its target polycount — pick a number that suits the
intended on-screen size. Tiny background buildings: 3-5 K. Hero buildings
(pubs, churches): 10-15 K.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CatalogueEntry:
    category: str           # "building" | "tree" | "vehicle"
    type: str               # OSM/asset type bucket
    slug: str               # filename-safe id, unique
    prompt: str             # text passed to Meshy
    footprint_m: tuple[float, float] = (10.0, 8.0)
    levels: int = 1
    art_style: str = "realistic"
    target_polycount: int = 6_000   # how detailed Meshy should make the mesh


_NI_BG = (
    "stylized game-ready low-poly building with painted facade textures, "
    "clean topology, simple block geometry, weathered exterior, "
    "Northern Ireland rural style, exterior only, daylight, white background"
)


# ---------------------------------------------------------------------------
# 10 buildings — covers the common OSM tags for rural NI
# ---------------------------------------------------------------------------
BUILDINGS: list[CatalogueEntry] = [
    CatalogueEntry("building", "residential", "ni_semi_detached",
                   f"a two storey semi-detached pebbledash house with slate roof, {_NI_BG}",
                   footprint_m=(12, 8), levels=2, target_polycount=6_000),
    CatalogueEntry("building", "residential", "ni_bungalow",
                   f"a single storey country bungalow with hipped slate roof and chimney, {_NI_BG}",
                   footprint_m=(14, 9), levels=1, target_polycount=5_000),
    CatalogueEntry("building", "residential", "ni_cottage",
                   f"a small traditional whitewashed Irish cottage with slate roof, {_NI_BG}",
                   footprint_m=(10, 6), levels=1, target_polycount=5_000),

    CatalogueEntry("building", "commercial", "ni_country_pub",
                   f"a small country pub or inn with hanging sign, painted exterior, slate roof, {_NI_BG}",
                   footprint_m=(14, 10), levels=2, target_polycount=10_000),
    CatalogueEntry("building", "shop", "ni_village_shop",
                   f"a single storey village shop with shopfront windows and signage, {_NI_BG}",
                   footprint_m=(10, 8), levels=1, target_polycount=7_000),

    CatalogueEntry("building", "civic", "ni_parish_church",
                   f"a small parish church with stone walls, slate roof, modest belltower, {_NI_BG}",
                   footprint_m=(20, 10), levels=2, target_polycount=12_000),
    CatalogueEntry("building", "civic", "ni_school",
                   f"a small rural primary school, single storey red brick with large windows, {_NI_BG}",
                   footprint_m=(25, 12), levels=1, target_polycount=8_000),

    CatalogueEntry("building", "barn", "ni_stone_barn",
                   f"a traditional stone barn with corrugated metal roof, weathered wooden door, {_NI_BG}",
                   footprint_m=(12, 7), levels=1, target_polycount=5_000),
    CatalogueEntry("building", "shed", "ni_machinery_shed",
                   f"an open-fronted three-bay machinery shed with corrugated metal roof, {_NI_BG}",
                   footprint_m=(18, 9), levels=1, target_polycount=4_000),
    # Industrial shed dropped from the catalogue (rare in pure rural NI bboxes;
    # OSM industrial=yes falls back to ni_machinery_shed via the type aliases)
]


# ---------------------------------------------------------------------------
# 2 trees — broadleaf + conifer
# ---------------------------------------------------------------------------
_TREE_BG = (
    "stylized game-ready low-poly tree, chunky textured bark trunk, "
    "alpha-cutout layered leaf canopy in cross-pattern paddle shape, "
    "clean clean topology, white background, daylight, ready for video game placement"
)

TREES: list[CatalogueEntry] = [
    # Trees are billboarded beyond ~600 m and there are 1000+ instances per
    # map — keep them tiny. 1.2K-1.5K is plenty for the 250 close GLB clones.
    CatalogueEntry("tree", "oak", "tree_oak",
                   f"mature oak tree with broad leafy canopy, {_TREE_BG}",
                   footprint_m=(8, 8), levels=1, target_polycount=1_500),
    CatalogueEntry("tree", "spruce", "tree_sitka_spruce",
                   f"tall sitka spruce conifer with conical canopy, {_TREE_BG}",
                   footprint_m=(5, 5), levels=1, target_polycount=1_200),
    # Hawthorn — the iconic field-boundary / hedgerow tree in rural NI.
    # Smaller and gnarlier than oak. Goes well at hedge edges and lone in fields.
    CatalogueEntry("tree", "hawthorn", "tree_hawthorn",
                   f"small gnarled hawthorn tree with sparse rounded canopy, {_TREE_BG}",
                   footprint_m=(4, 4), levels=1, target_polycount=1_200),
]


VEHICLES: list[CatalogueEntry] = []   # deferred for now


CATALOGUE: list[CatalogueEntry] = BUILDINGS + TREES + VEHICLES


def by_category(category: str) -> list[CatalogueEntry]:
    return [e for e in CATALOGUE if e.category == category]


def total_count() -> int:
    return len(CATALOGUE)


# ---------------------------------------------------------------------------
# Per-slug prompt overrides — persisted in mapng_ai/cache/prompt_overrides.json
# ---------------------------------------------------------------------------
import json
from pathlib import Path
from mapng_ai import config


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
    data = {}
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
    """The prompt the runner will actually send to Meshy — override wins."""
    return get_prompt_override(entry.slug) or entry.prompt

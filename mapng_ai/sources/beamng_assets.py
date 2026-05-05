"""BeamNG asset reference scanner.

When MAPNG_BEAMNG_PATH is set, walks the user's installed BeamNG levels
and catalogues every `.dae` shape we could reference from a generated
level. The pipeline can then emit `TSStatic { shapeName: "/levels/..." }`
that resolves against the user's BeamNG install at run time — no asset
duplication, no ZIP bloat, and the visual matches the rest of BeamNG.

Licence note: BeamNG-shipped assets are referenced by path here. We do
NOT extract or redistribute them. The user's BeamNG install supplies
the meshes when they load the generated level.

Two storage layouts are supported:
  1. Extracted: `<install>/levels/<lvl>/art/shapes/...dae` on disk
  2. Zipped (shipping default): `<install>/content/levels/<lvl>.zip`
     where members live at `levels/<lvl>/art/shapes/...dae`
"""
from __future__ import annotations

import os
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path


_DEFAULT_LEVELS = (
    # Italy ONLY — its rural Mediterranean style (stone walls, small village
    # houses, churches, dirt tracks) is the closest vanilla match for NI
    # countryside. Other levels brought in mismatched buildings (e.g. ECA
    # American houses, Asian-DLC kanji shop signs).
    "italy",
)


# Curated whitelist of Italy assets safe for placement on a generated map.
# Sizes are MEASURED from real DAE bounding boxes via trimesh — see
# italy_bounds.py and italy_bounds.json. The previous "hand-eyeball
# the size" approach was wildly off (e.g. palazzo eyeballed at 20m,
# actual size 56m → 3× scaling error → buildings appearing 100× too big).

# (relpath, type)
_ITALY_BUILDINGS_RAW: tuple[tuple[str, str], ...] = (
    # 16 town/village houses
    ("/levels/italy/art/shapes/buildings/italy_town_bld1.dae",  "residential"),
    ("/levels/italy/art/shapes/buildings/italy_town_bld2.dae",  "residential"),
    ("/levels/italy/art/shapes/buildings/italy_town_bld3.dae",  "residential"),
    ("/levels/italy/art/shapes/buildings/italy_town_bld4.dae",  "residential"),
    ("/levels/italy/art/shapes/buildings/italy_town_bld5.dae",  "residential"),
    ("/levels/italy/art/shapes/buildings/italy_town_bld6.dae",  "shop"),
    ("/levels/italy/art/shapes/buildings/italy_town_bld7.dae",  "shop"),
    ("/levels/italy/art/shapes/buildings/italy_town_bld8.dae",  "shop"),
    ("/levels/italy/art/shapes/buildings/italy_town_bld9.dae",  "residential"),
    ("/levels/italy/art/shapes/buildings/italy_town_bld10.dae", "shop"),
    ("/levels/italy/art/shapes/buildings/italy_town_bld11.dae", "residential"),
    ("/levels/italy/art/shapes/buildings/italy_town_bld12.dae", "shop"),
    ("/levels/italy/art/shapes/buildings/italy_town_bld13.dae", "apartment"),
    ("/levels/italy/art/shapes/buildings/italy_town_bld14.dae", "apartment"),
    ("/levels/italy/art/shapes/buildings/italy_town_bld15.dae", "apartment"),
    ("/levels/italy/art/shapes/buildings/italy_town_bld16.dae", "apartment"),
    # Apartment + small church (palazzo + mansion + large church dropped:
    # they measured 50m+ across, way too big to represent NI village blocks)
    ("/levels/italy/art/shapes/buildings/italy_bld_20x12_apartment.dae", "apartment"),
    ("/levels/italy/art/shapes/buildings/italy_bld_small_church.dae",    "civic"),
)

# (relpath, type)
_ITALY_TREES_RAW: tuple[tuple[str, str], ...] = (
    ("/levels/italy/art/shapes/trees/trees_italy/holm_oak.dae",           "oak"),
    ("/levels/italy/art/shapes/trees/trees_italy/holm_oak_city_small.dae", "oak"),
    ("/levels/italy/art/shapes/trees/trees_italy/holm_oak_city_tall.dae",  "oak"),
    ("/levels/italy/art/shapes/trees/trees_italy/maritime_pine.dae",       "pine"),
    ("/levels/italy/art/shapes/trees/trees_italy/maritime_pine_2.dae",     "pine"),
    ("/levels/italy/art/shapes/trees/trees_italy/scots_pine.dae",          "pine"),
    ("/levels/italy/art/shapes/trees/trees_italy/cork_oak_medium.dae",     "oak"),
    ("/levels/italy/art/shapes/trees/trees_italy/cork_oak_large_1.dae",    "oak"),
    ("/levels/italy/art/shapes/trees/trees_italy/generibush.dae",          "bush"),
    ("/levels/italy/art/shapes/trees/trees_italy/fluffy_bush.dae",         "bush"),
)


def _resolve_italy_sizes() -> tuple[
    tuple[tuple[str, str, float, float, float], ...],
    tuple[tuple[str, str, float, float, float], ...],
]:
    """Read italy_bounds.json cache; build whitelists with real measurements.
    Skips any asset whose bounds didn't measure properly (fallback to None
    is filtered out)."""
    try:
        from mapng_ai.sources.italy_bounds import get_or_measure
    except ImportError:
        return ((), ())
    all_paths = [p for p, _ in _ITALY_BUILDINGS_RAW] + [p for p, _ in _ITALY_TREES_RAW]
    measured = get_or_measure(all_paths)
    DEFAULT = (10.0, 8.0, 5.0)
    buildings = tuple(
        (p, t, *measured[p]) for p, t in _ITALY_BUILDINGS_RAW
        if measured.get(p) and measured[p] != DEFAULT
    )
    trees = tuple(
        (p, t, *measured[p]) for p, t in _ITALY_TREES_RAW
        if measured.get(p) and measured[p] != DEFAULT
    )
    return buildings, trees


_ITALY_BUILDINGS, _ITALY_TREES = _resolve_italy_sizes()


@dataclass(frozen=True)
class BeamNGAsset:
    level: str                         # source level dir name
    relpath: str                       # in-zip relpath e.g. /levels/<lvl>/art/shapes/<...>.dae
    fs_path: Path                      # absolute path on disk
    category: str                      # 'building' | 'tree' | 'vehicle' | 'unknown'
    type: str                          # type bucket
    natural_size_m: tuple[float, float, float]   # length, width, height — best guess


def _looks_like_install(p: Path) -> bool:
    """Real BeamNG installs ship `BeamNG.drive.exe` + a `content/` folder
    (for zipped levels) or a `levels/` folder (older / portable installs).
    The userdata dir under %LOCALAPPDATA% has neither, so it's filtered."""
    if not p.exists() or not p.is_dir():
        return False
    if (p / "BeamNG.drive.exe").exists():
        return True
    if (p / "content" / "levels").exists():
        return True
    if (p / "levels").exists():
        # Reject the userdata layout where `levels/` contains only mods.
        # Game installs ship at least one shipped level dir or zip.
        return any((p / "levels").iterdir())
    return False


def _install_path() -> Path | None:
    p = os.environ.get("MAPNG_BEAMNG_PATH")
    if p and _looks_like_install(Path(p)):
        return Path(p)
    # Try common Steam install locations FIRST — the actual content lives
    # there. The userdata folder under %LOCALAPPDATA% only has logs +
    # vehicles + mods, no shipped levels, so it's a poor source.
    candidates = [
        Path("C:/Program Files (x86)/Steam/steamapps/common/BeamNG.drive"),
        Path("C:/Program Files/Steam/steamapps/common/BeamNG.drive"),
        Path("D:/SteamLibrary/steamapps/common/BeamNG.drive"),
        Path("D:/Steam/steamapps/common/BeamNG.drive"),
        Path("E:/SteamLibrary/steamapps/common/BeamNG.drive"),
        Path("F:/SteamLibrary/steamapps/common/BeamNG.drive"),
        Path(os.path.expandvars(r"%LOCALAPPDATA%/BeamNG.drive")),
    ]
    for guess in candidates:
        if _looks_like_install(guess):
            return guess
    return None


def _classify_path(relpath: str) -> tuple[str, str]:
    p = relpath.lower()
    if "/buildings/" in p or "/building/" in p or "/houses/" in p or "/house/" in p:
        return "building", "default"
    if "/trees/" in p or "/foliage/" in p or "/forest/" in p:
        return "tree", "default"
    if "/vehicles/" in p or "/cars/" in p or "/props_vehicles" in p:
        return "vehicle", "default"
    return "unknown", "default"


def _refine_type(relpath: str, category: str) -> str:
    p = relpath.lower()
    if category == "building":
        if any(w in p for w in ("cottage", "house", "home", "cabin")): return "residential"
        if any(w in p for w in ("barn",)): return "barn"
        if any(w in p for w in ("shed",)): return "shed"
        if any(w in p for w in ("church", "school")): return "civic"
        if any(w in p for w in ("shop", "store", "diner")): return "shop"
        if any(w in p for w in ("warehouse", "factory", "industrial")): return "industrial"
        if any(w in p for w in ("garage",)): return "garage"
        if any(w in p for w in ("gas_station", "petrol")): return "commercial"
        return "default"
    if category == "tree":
        if "oak" in p: return "oak"
        if "spruce" in p or "fir" in p: return "spruce"
        if "pine" in p: return "pine"
        if "birch" in p: return "birch"
        return "default"
    return "default"


def _scan_extracted(levels_root: Path, targets: set[str]) -> list[BeamNGAsset]:
    out: list[BeamNGAsset] = []
    if not levels_root.exists():
        return out
    for level_dir in levels_root.iterdir():
        if not level_dir.is_dir() or level_dir.name not in targets:
            continue
        shapes_dir = level_dir / "art" / "shapes"
        if not shapes_dir.exists():
            continue
        for path in shapes_dir.rglob("*.dae"):
            rel = path.relative_to(level_dir).as_posix()
            in_zip = f"/levels/{level_dir.name}/{rel}"
            cat, _ = _classify_path(in_zip)
            if cat == "unknown":
                continue
            out.append(BeamNGAsset(
                level=level_dir.name,
                relpath=in_zip,
                fs_path=path,
                category=cat,
                type=_refine_type(in_zip, cat),
                natural_size_m=(10.0, 8.0, 5.0),
            ))
    return out


def _scan_zipped(content_levels: Path, targets: set[str]) -> list[BeamNGAsset]:
    """BeamNG ships levels as `content/levels/<lvl>.zip`. Members are
    rooted at `levels/<lvl>/...` so we can reference the path directly
    against BeamNG's virtual filesystem at run time. We never extract.
    """
    out: list[BeamNGAsset] = []
    if not content_levels.exists():
        return out
    for zpath in content_levels.glob("*.zip"):
        level_name = zpath.stem
        if level_name not in targets:
            continue
        try:
            with zipfile.ZipFile(zpath) as zf:
                for member in zf.namelist():
                    if not member.endswith(".dae"):
                        continue
                    # Members are typed as e.g.
                    # 'levels/west_coast_usa/art/shapes/...dae'.
                    if "/art/shapes/" not in member:
                        continue
                    in_zip = "/" + member.lstrip("/")
                    cat, _ = _classify_path(in_zip)
                    if cat == "unknown":
                        continue
                    out.append(BeamNGAsset(
                        level=level_name,
                        relpath=in_zip,
                        fs_path=zpath,         # zip itself; we never read it
                        category=cat,
                        type=_refine_type(in_zip, cat),
                        natural_size_m=(10.0, 8.0, 5.0),
                    ))
        except (zipfile.BadZipFile, OSError):
            continue
    return out


def scan(levels: tuple[str, ...] | None = None) -> list[BeamNGAsset]:
    """Return curated Italy assets with hand-tuned natural sizes.

    The previous "scan everything in /buildings/" approach pulled in
    billboards, bridges, dams, fire stations, etc. — all of which got
    scaled to OSM residential footprints and produced absurd 100×-too-big
    structures. Now we ONLY emit assets from the curated whitelist with
    manually-specified natural sizes, so the placement scaler's
    `scale = footprint / natural_size` math produces sane results.
    """
    install = _install_path()
    if install is None:
        return []

    # Italy zip exists check
    italy_zip = install / "content" / "levels" / "italy.zip"
    italy_extracted = install / "levels" / "italy"
    if not italy_zip.is_file() and not italy_extracted.is_dir():
        return []

    fs_anchor = italy_zip if italy_zip.is_file() else italy_extracted
    out: list[BeamNGAsset] = []
    for relpath, btype, l, w, h in _ITALY_BUILDINGS:
        out.append(BeamNGAsset(
            level="italy", relpath=relpath, fs_path=fs_anchor,
            category="building", type=btype,
            natural_size_m=(float(l), float(w), float(h)),
        ))
    for relpath, ttype, l, w, h in _ITALY_TREES:
        out.append(BeamNGAsset(
            level="italy", relpath=relpath, fs_path=fs_anchor,
            category="tree", type=ttype,
            natural_size_m=(float(l), float(w), float(h)),
        ))
    return out


_CACHED_SCAN: list[BeamNGAsset] | None = None


def cached_scan() -> list[BeamNGAsset]:
    global _CACHED_SCAN
    if _CACHED_SCAN is None:
        _CACHED_SCAN = scan()
    return _CACHED_SCAN


def reset_scan_cache() -> None:
    global _CACHED_SCAN
    _CACHED_SCAN = None


def install_status() -> dict:
    """For the UI: where BeamNG was found and how many shapes are usable."""
    install = _install_path()
    if install is None:
        return {"detected": False, "path": None, "asset_count": 0,
                "by_category": {}}
    assets = cached_scan()
    by_cat: dict[str, int] = {}
    by_lvl: dict[str, int] = {}
    for a in assets:
        by_cat[a.category] = by_cat.get(a.category, 0) + 1
        by_lvl[a.level] = by_lvl.get(a.level, 0) + 1
    return {
        "detected": True,
        "path": str(install),
        "asset_count": len(assets),
        "by_category": by_cat,
        "by_level": by_lvl,
    }

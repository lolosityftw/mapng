"""CC0 asset pack importer.

Drops a zip file from Quaternius / Kenney / Sketchfab onto the Asset
Browser → unpacks → walks the contents → infers a category for each
.glb/.gltf/.dae by filename heuristics → copies into
`assets/<category>/<type>/<sluggified_name>.<ext>`.

Conservative on category inference: anything we can't confidently classify
gets `default` so the LibraryProvider's universal fallback still picks it
up. Manifest.json files are written/updated alongside.
"""
from __future__ import annotations

import json
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path

from mapng_ai import config


# (regex, category, type) — first match wins. Ordered most-specific-first.
_CATEGORY_PATTERNS: list[tuple[re.Pattern, str, str]] = [
    # Trees / foliage
    (re.compile(r"oak", re.I),                "tree", "oak"),
    (re.compile(r"spruce|conifer|fir", re.I), "tree", "spruce"),
    (re.compile(r"hawthorn|haw", re.I),       "tree", "hawthorn"),
    (re.compile(r"birch", re.I),              "tree", "birch"),
    (re.compile(r"willow", re.I),             "tree", "willow"),
    (re.compile(r"pine", re.I),               "tree", "pine"),
    (re.compile(r"maple", re.I),              "tree", "maple"),
    (re.compile(r"tree[_\- ]|^tree|tree$|trunk", re.I), "tree", "default"),
    (re.compile(r"bush|shrub|hedge", re.I),   "tree", "bush"),
    # Vehicles
    (re.compile(r"car[_\- ]|sedan|coupe", re.I), "vehicle", "car"),
    (re.compile(r"truck|lorry", re.I),        "vehicle", "lorry"),
    (re.compile(r"tractor", re.I),            "vehicle", "tractor"),
    (re.compile(r"bus[_\- ]|^bus", re.I),     "vehicle", "bus"),
    (re.compile(r"trailer", re.I),            "vehicle", "trailer"),
    # Buildings — residential
    (re.compile(r"cottage", re.I),            "building", "residential"),
    (re.compile(r"bungalow", re.I),           "building", "residential"),
    (re.compile(r"house|home|villa|residen", re.I), "building", "residential"),
    (re.compile(r"semi", re.I),               "building", "residential"),
    (re.compile(r"apartment|tenement", re.I), "building", "residential"),
    # Buildings — commercial / civic
    (re.compile(r"church|chapel|cathedral|temple", re.I), "building", "civic"),
    (re.compile(r"school|college|hospital", re.I),        "building", "civic"),
    (re.compile(r"hall|library|museum",   re.I),          "building", "civic"),
    (re.compile(r"shop|store|market|grocery|kiosk", re.I), "building", "shop"),
    (re.compile(r"pub|bar[_\- ]|inn|tavern|hotel", re.I),  "building", "commercial"),
    (re.compile(r"office|commercial",   re.I),             "building", "commercial"),
    # Buildings — agricultural / industrial
    (re.compile(r"barn", re.I),               "building", "barn"),
    (re.compile(r"shed|stable|cowshed",  re.I),          "building", "shed"),
    (re.compile(r"warehouse|industrial|factory", re.I),  "building", "industrial"),
    (re.compile(r"silo", re.I),               "building", "industrial"),
    (re.compile(r"garage", re.I),             "building", "garage"),
    # Generic building fallback (must be last)
    (re.compile(r"building|structure", re.I), "building", "default"),
]


_VALID_EXTS = {".glb", ".gltf", ".dae", ".fbx"}


@dataclass(frozen=True)
class ImportResult:
    imported: int
    skipped: int
    by_category: dict[str, int]
    errors: list[str]


def _slugify(name: str) -> str:
    base = re.sub(r"[^a-zA-Z0-9_-]+", "_", name).strip("_")
    return base.lower() or "asset"


def _classify(filename: str) -> tuple[str, str]:
    """Return (category, type) for a filename. Falls back to ('building', 'default')."""
    for pat, cat, typ in _CATEGORY_PATTERNS:
        if pat.search(filename):
            return cat, typ
    # Unknown — assume building/default so it still gets used via
    # LibraryProvider's universal fallback chain
    return "building", "default"


def _target_dir(category: str, type_: str) -> Path:
    return config.ROOT / "assets" / f"{category}s" / type_


def _update_manifest(folder: Path, slug: str, footprint: tuple[float, float], levels: int) -> None:
    p = folder / "manifest.json"
    data: dict = {}
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    glb_name = f"{slug}.glb"
    data[glb_name] = {
        "slug": slug,
        "footprint_m": list(footprint),
        "levels": levels,
        "source": "imported",
    }
    p.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


def import_zip(zip_bytes: bytes) -> ImportResult:
    """Extract a zip, sort .glb/.gltf/.dae files by inferred category, and
    copy them into assets/<category>s/<type>/. Returns a result summary."""
    imported = 0
    skipped = 0
    by_cat: dict[str, int] = {}
    errors: list[str] = []

    import io
    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile as exc:
        return ImportResult(0, 0, {}, [f"not a zip: {exc}"])

    # Pre-scan for .glb files so we know what to import
    candidates: list[zipfile.ZipInfo] = []
    for info in zf.infolist():
        if info.is_dir():
            continue
        ext = Path(info.filename).suffix.lower()
        if ext == ".glb" or ext == ".gltf":
            candidates.append(info)

    if not candidates:
        errors.append("no .glb / .gltf files found in archive")
        return ImportResult(0, 0, {}, errors)

    for info in candidates:
        try:
            name = Path(info.filename).name        # strip directory prefixes
            stem = Path(name).stem
            ext = Path(name).suffix.lower()
            cat, typ = _classify(stem)
            slug = _slugify(stem)
            folder = _target_dir(cat, typ)
            folder.mkdir(parents=True, exist_ok=True)
            out = folder / f"{slug}{ext}"
            if out.exists():
                skipped += 1
                continue
            with zf.open(info) as src, out.open("wb") as dst:
                dst.write(src.read())
            # Conservative footprint guess; trimesh would give exact extents
            # but adds a slow load step. Manifest can be hand-edited later.
            _update_manifest(folder, slug, footprint=(10.0, 8.0), levels=1)
            imported += 1
            by_cat[f"{cat}/{typ}"] = by_cat.get(f"{cat}/{typ}", 0) + 1
        except Exception as exc:
            errors.append(f"{info.filename}: {exc}")
    return ImportResult(imported=imported, skipped=skipped, by_category=by_cat, errors=errors)

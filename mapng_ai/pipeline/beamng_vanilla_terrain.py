"""Extract vanilla BeamNG TerrainMaterial pack into a mod.

Why this exists: BeamNG forum guidance is unambiguous — to use stock terrain
materials in a mod, you must COPY the texture files into your mod's art
folder and edit the material paths to fit your folder structure. You cannot
just reference /levels/industrial/... from a mod context (the textures
won't be found at terrain init time).

What this does:
  1. Locates Industrial.zip + content/assets/materials/terrain.zip
  2. Reads the materials we want for our 8 land classes
  3. For each texture path, finds the actual PNG bytes by:
       - reading directly from Industrial.zip if a real .png is present
       - else following the .png.link redirect into terrain.zip
  4. Bundles ALL textures into our mod at /levels/{our_level}/art/terrains/
     and rewrites every material path to point there. We do NOT rely on
     /assets/materials/terrain/... being mounted in the mod's VFS context
     (empirically that fails with "Material X is missing texture").
  5. Returns (materials_dict, files_to_bundle) ready to ship
"""
from __future__ import annotations

import json
import uuid
import zipfile
from pathlib import Path
from typing import Dict, List, Tuple


# Land class → vanilla internalName. We prefer Italy's terrain pack —
# it's a Mediterranean countryside level with 4 grass variants, weathered
# rural roads, dirt tracks, and forest_floor — much closer match to NI
# rural villages than Industrial (which is a race-track / concrete yard).
# Falls back to Industrial materials if Italy isn't installed.
CLASS_TO_ITALY: Dict[str, str] = {
    "asphalt":  "groundmodel_asphalt1",  # main rural roads
    "concrete": "asphalt2",              # weathered/cracked asphalt — closest to NI village concrete
    "lawn":     "Grass3",                # vivid managed lawn
    "pasture":  "Grass2",                # rougher pasture green
    "earth":    "dirt_loose",            # unpaved farm tracks
    "gravel":   "RockyDirt",             # gravel/rocky tracks
    "water":    "BeachSand",             # WaterPlane handles actual water
    "forest":   "forest_floor",          # leaf litter / woodland
}

CLASS_TO_INDUSTRIAL: Dict[str, str] = {
    "asphalt":  "groundmodel_asphalt1",
    "concrete": "Concrete",
    "lawn":     "Grass3",
    "pasture":  "Grass2",
    "earth":    "dirt",
    "gravel":   "Gravel",
    "water":    "BeachSand",
    "forest":   "forest_floor",
}


# All texture-pointing field names in BeamNG TerrainMaterial JSON.
_TEX_FIELDS = (
    "diffuseMap", "detailMap", "macroMap",
    "normalMap", "normalDetailMap", "normalMacroMap",
    "baseColorBaseTex", "baseColorDetailTex", "baseColorMacroTex",
    "normalBaseTex", "normalDetailTex", "normalMacroTex",
    "roughnessBaseTex", "roughnessDetailTex", "roughnessMacroTex",
    "aoBaseTex", "aoDetailTex", "aoMacroTex",
    "heightBaseTex", "heightDetailTex", "heightMacroTex",
    "specularMap",
)


def _candidate_install_bases() -> List[Path]:
    """Common BeamNG install locations."""
    drives = ["C:", "D:", "E:", "F:"]
    bases = [
        "/SteamLibrary/steamapps/common/BeamNG.drive",
        "/Program Files (x86)/Steam/steamapps/common/BeamNG.drive",
        "/Program Files/Steam/steamapps/common/BeamNG.drive",
        "/Games/BeamNG.drive",
    ]
    out: List[Path] = []
    for drv in drives:
        for base in bases:
            out.append(Path(drv + base))
    return out


def find_level_zip(level_zip_name: str, override: Path | str | None = None) -> Path | None:
    """Locate a vanilla level zip (e.g. 'italy.zip', 'Industrial.zip')."""
    if override is not None:
        p = Path(override)
        return p if p.is_file() else None
    for base in _candidate_install_bases():
        p = base / "content" / "levels" / level_zip_name
        try:
            if p.is_file():
                return p
        except OSError:
            continue
    return None


def find_industrial_zip(override: Path | str | None = None) -> Path | None:
    return find_level_zip("Industrial.zip", override)


def find_italy_zip(override: Path | str | None = None) -> Path | None:
    # Italy is named lowercase in some installs
    p = find_level_zip("italy.zip", override)
    if p is not None:
        return p
    return find_level_zip("Italy.zip", override)


def find_terrain_assets_zip(level_zip: Path) -> Path | None:
    """Locate the central terrain.zip relative to a level zip path."""
    base = level_zip.parent.parent  # .../content
    p = base / "assets" / "materials" / "terrain.zip"
    return p if p.is_file() else None


def _build_redirect_map(zf: zipfile.ZipFile) -> Dict[str, str]:
    """Read every *.png.link file in the zip and map logical→real paths."""
    redirects: Dict[str, str] = {}
    for name in zf.namelist():
        if not name.endswith(".png.link"):
            continue
        try:
            d = json.loads(zf.read(name))
        except (json.JSONDecodeError, KeyError):
            continue
        # Logical path is "/<name without .link>" with case preserved as in
        # the materials.json (Industrial uses lowercase 'industrial' in
        # texture refs but uppercase 'Industrial' in zip member names).
        logical = "/" + name[:-5]
        target = d.get("path", "")
        if target:
            # Index by both lowercase logical and the actual case
            redirects[logical.lower()] = target
            redirects[logical] = target
    return redirects


def _find_member_ci(zf: zipfile.ZipFile, path: str) -> str | None:
    """Case-insensitive zip member lookup."""
    target = path.lower()
    for member in zf.namelist():
        if member.lower() == target:
            return member
    return None


def _resolve_texture_bytes(
    tex_path: str,
    industrial_zf: zipfile.ZipFile,
    industrial_redirects: Dict[str, str],
    terrain_zf: zipfile.ZipFile | None,
) -> bytes | None:
    """Find and return the actual PNG bytes for a texture path.

    Resolution order:
      1. If the path exists as a real .png in Industrial.zip, read it
      2. If a .png.link exists, follow the redirect into terrain.zip
      3. Try terrain.zip directly (for /assets/... paths)
      Returns None if nothing found.
    """
    # 1. Real PNG in Industrial.zip
    src_logical = tex_path.lstrip("/")
    member = _find_member_ci(industrial_zf, src_logical)
    if member is not None:
        return industrial_zf.read(member)

    # 2. Follow .png.link redirect
    target = (industrial_redirects.get(tex_path)
              or industrial_redirects.get(tex_path.lower()))
    if target and terrain_zf is not None:
        target_logical = target.lstrip("/")
        member = _find_member_ci(terrain_zf, target_logical)
        if member is not None:
            return terrain_zf.read(member)

    # 3. Direct lookup in terrain.zip (for paths already in /assets/ form)
    if terrain_zf is not None:
        member = _find_member_ci(terrain_zf, src_logical)
        if member is not None:
            return terrain_zf.read(member)

    return None


def build_vanilla_terrain_pack(
    level_name: str,
    classes_used: List[str],
    industrial_zip: Path | None = None,
    side_m: float = 1024.0,
    source: str = "italy",  # "italy" or "industrial" — Italy is rural Mediterranean countryside (best fit for NI villages)
) -> Tuple[Dict[str, dict], List[Tuple[str, bytes]]] | None:
    """Build a vanilla terrain material pack for the given list of class keys.

    Bundles all texture files into our mod (both level-local PNGs and
    redirected /assets/... PNGs from terrain.zip).

    source: which vanilla level to clone materials from. Italy is the
    default — its 4 grass variants and weathered rural roads match NI
    countryside much better than Industrial's race-track materials.
    """
    # Pick source level
    if source == "italy":
        src_zip = find_italy_zip()
        members = ("levels/italy/art/terrains/main.materials.json",
                   "levels/Italy/art/terrains/main.materials.json")
        class_map = CLASS_TO_ITALY
    else:
        src_zip = industrial_zip or find_industrial_zip()
        members = ("levels/Industrial/art/terrains/main.materials.json",
                   "levels/industrial/art/terrains/main.materials.json")
        class_map = CLASS_TO_INDUSTRIAL

    # Italy fallback to Industrial if Italy not installed
    if src_zip is None and source == "italy":
        src_zip = find_industrial_zip()
        members = ("levels/Industrial/art/terrains/main.materials.json",
                   "levels/industrial/art/terrains/main.materials.json")
        class_map = CLASS_TO_INDUSTRIAL

    if src_zip is None:
        return None
    terrain_zip = find_terrain_assets_zip(src_zip)

    try:
        izf = zipfile.ZipFile(src_zip, "r")
    except (zipfile.BadZipFile, OSError):
        return None
    tzf = None
    if terrain_zip is not None:
        try:
            tzf = zipfile.ZipFile(terrain_zip, "r")
        except (zipfile.BadZipFile, OSError):
            tzf = None

    try:
        mats_raw = None
        for member_path in members:
            try:
                mats_raw = json.loads(izf.read(member_path))
                break
            except KeyError:
                continue
        if mats_raw is None:
            return None

        redirects = _build_redirect_map(izf)

        wanted_internal: Dict[str, str] = {}
        for cls_key in classes_used:
            iname = class_map.get(cls_key)
            if iname:
                wanted_internal[cls_key] = iname

        by_internal: Dict[str, dict] = {}
        for k, v in mats_raw.items():
            inner = v.get("internalName")
            if inner:
                by_internal[inner] = v

        out_materials: Dict[str, dict] = {}
        bundle_files: Dict[str, bytes] = {}
        our_terrain_dir = f"/levels/{level_name}/art/terrains"

        # ---- TerrainMaterialTextureSet ----
        # PBR TerrainMaterials REQUIRE a companion TerrainMaterialTextureSet
        # object that defines the base/detail/macro atlas sizes. Without it
        # BeamNG silently fails to bind textures even though the material
        # itself loads — you get "Material X is missing texture" forever.
        # We clone Industrial's textureSet, give it a unique name + UUID,
        # and reference it from the TerrainBlock via materialTextureSet.
        ts_pid = str(uuid.uuid5(
            uuid.NAMESPACE_URL, f"mapng:terrain_textureset:{level_name}"
        ))
        ts_name = f"MapNG_terrainTextureSet_{level_name}"
        # Pull from Industrial's textureSet if present, else use 1024 defaults
        ts_template = None
        for k, v in mats_raw.items():
            if v.get("class") == "TerrainMaterialTextureSet":
                ts_template = v
                break
        # Our terrain.png is the satellite/OSM composite — typically 2048×2048.
        # Override the TextureSet's base size to match (Industrial uses 1024×1024
        # because its base textures are 1024 pixels; ours are bigger).
        texture_set = {
            "name": ts_name,
            "class": "TerrainMaterialTextureSet",
            "persistentId": ts_pid,
            "baseTexSize":   [2048, 2048],
            "detailTexSize": ts_template["detailTexSize"] if ts_template else [1024, 1024],
            "macroTexSize":  ts_template["macroTexSize"]  if ts_template else [1024, 1024],
        }
        out_materials[ts_name] = texture_set

        for cls_key, iname in wanted_internal.items():
            tmpl = by_internal.get(iname)
            if tmpl is None:
                continue
            mat = dict(tmpl)

            new_internal = f"MapNG_{iname}"
            mat["internalName"] = new_internal
            pid = str(uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"mapng:terrain:{level_name}:{new_internal}",
            ))
            mat["persistentId"] = pid
            # Keep Industrial's diffuseSize/detailSize/macroSize — those are
            # tuned for the per-material textures we're now using as base.

            for fk in _TEX_FIELDS:
                tex_path = mat.get(fk)
                if not isinstance(tex_path, str):
                    continue
                blob = _resolve_texture_bytes(tex_path, izf, redirects, tzf)
                if blob is None:
                    mat.pop(fk, None)
                    continue
                fname = tex_path.lstrip("/").rsplit("/", 1)[-1]
                bundle_relpath = f"levels/{level_name}/art/terrains/{fname}"
                if bundle_relpath not in bundle_files:
                    bundle_files[bundle_relpath] = blob
                mat[fk] = f"{our_terrain_dir}/{fname}"

            # Industrial's `t_terrain_base_b.png` is a RENDERED image of
            # Industrial's actual level (race track, buildings, etc.) —
            # NOT a generic tileable texture. Reusing it on our map shows
            # Industrial's race track tiled.
            #
            # The PROPER BeamNG approach: each TerrainMaterial has its own
            # distinct base texture matching that material's character
            # (grass material → grass base, asphalt → asphalt base, etc.).
            # The .ter layerMap tells BeamNG which material to render
            # per-pixel; BeamNG blends between adjacent materials at
            # boundaries. Detail and macro layers add close/mid-range
            # variation on top of the per-material base.
            #
            # We achieve this by reusing the bundled per-material DETAIL
            # texture as the BASE texture (it's already a tileable pattern
            # of grass/asphalt/dirt/etc., and we already shipped it).
            detail_tex = mat.get("baseColorDetailTex")
            if isinstance(detail_tex, str):
                mat["baseColorBaseTex"] = detail_tex
                mat["baseColorBaseTexSize"] = mat.get("baseColorDetailTexSize", 1024)

            out_materials[f"{new_internal}-{pid}"] = mat

        files = [(p, b) for p, b in bundle_files.items()]
        return out_materials, files
    finally:
        izf.close()
        if tzf is not None:
            tzf.close()


def texture_set_name(level_name: str) -> str:
    """Return the TerrainMaterialTextureSet name for this level (used by
    the TerrainBlock's materialTextureSet field)."""
    return f"MapNG_terrainTextureSet_{level_name}"


def class_to_internal_name(level_name: str, cls_key: str, source: str = "italy") -> str | None:
    """Return the prefixed internalName our vanilla pack uses for this class.

    Used by the .ter writer to put the right material name in the binary.
    Must match the source the pack was built from.
    """
    if source == "italy":
        iname = CLASS_TO_ITALY.get(cls_key) or CLASS_TO_INDUSTRIAL.get(cls_key)
    else:
        iname = CLASS_TO_INDUSTRIAL.get(cls_key)
    return f"MapNG_{iname}" if iname else None

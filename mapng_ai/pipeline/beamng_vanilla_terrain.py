"""Extract vanilla BeamNG TerrainMaterial pack into a mod.

Why this exists: BeamNG forum guidance is unambiguous — to use stock terrain
materials in a mod, you must COPY the texture files into your mod's art
folder and edit the material paths to fit your folder structure. You cannot
just reference /levels/industrial/... from a mod context (the textures
won't be found at terrain init time).

What this does:
  1. Locates Industrial.zip on disk (highest-quality vanilla PBR set)
  2. Reads the materials we want for our 8 land classes
  3. For each texture path:
       - .png.link redirect → use the resolved /assets/materials/terrain/...
         path directly (those are in BeamNG's central terrain.zip which is
         always mounted in the VFS, so we don't need to bundle)
       - real .png file in Industrial.zip → extract its bytes and bundle
         them in our mod at /levels/{our_level}/art/terrains/, rewrite path
  4. Returns a (materials_dict, files_to_bundle) tuple ready to ship
"""
from __future__ import annotations

import json
import uuid
import zipfile
from pathlib import Path
from typing import Dict, List, Tuple


# Land class → Industrial internalName. Picks the Industrial materials with
# the best fit for our NI/Irish rural-village coverage. Industrial has full
# PBR base+detail+macro layers for all of these.
CLASS_TO_INDUSTRIAL: Dict[str, str] = {
    "asphalt":  "groundmodel_asphalt1",
    "concrete": "Concrete",
    "lawn":     "Grass3",
    "pasture":  "Grass2",
    "earth":    "dirt",
    "gravel":   "Gravel",
    "water":    "BeachSand",   # WaterPlane handles the actual water
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


def _candidate_industrial_paths() -> List[Path]:
    """Common locations users have BeamNG.drive installed."""
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
            out.append(Path(drv + base) / "content" / "levels" / "Industrial.zip")
    return out


def find_industrial_zip(override: Path | str | None = None) -> Path | None:
    """Locate the user's Industrial.zip. Returns None if not found."""
    if override is not None:
        p = Path(override)
        return p if p.is_file() else None
    for p in _candidate_industrial_paths():
        try:
            if p.is_file():
                return p
        except OSError:
            continue
    return None


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


def build_vanilla_terrain_pack(
    level_name: str,
    classes_used: List[str],
    industrial_zip: Path | None = None,
) -> Tuple[Dict[str, dict], List[Tuple[str, bytes]]] | None:
    """Build a vanilla terrain material pack for the given list of class keys.

    Args:
        level_name: the mod's level directory name (used to rewrite paths).
        classes_used: list of land class keys (e.g. ["asphalt", "lawn", "earth"]).
        industrial_zip: optional override path to Industrial.zip.

    Returns:
        (materials_dict, files_to_bundle) where:
          - materials_dict: TerrainMaterial defs keyed by "<internalName>-<uuid>"
            with all texture paths rewritten for OUR mod
          - files_to_bundle: list of (zip_relpath, bytes) for textures that
            must be physically copied into our mod zip
        Returns None if Industrial.zip isn't found.
    """
    industrial_zip = industrial_zip or find_industrial_zip()
    if industrial_zip is None:
        return None

    try:
        zf = zipfile.ZipFile(industrial_zip, "r")
    except (zipfile.BadZipFile, OSError):
        return None

    with zf:
        try:
            mats_raw = json.loads(zf.read(
                "levels/Industrial/art/terrains/main.materials.json"
            ))
        except KeyError:
            return None

        # Build the redirect map once
        redirects = _build_redirect_map(zf)

        # Find which Industrial materials we need
        wanted_internal: Dict[str, str] = {}  # class_key -> internalName
        for cls_key in classes_used:
            iname = CLASS_TO_INDUSTRIAL.get(cls_key)
            if iname:
                wanted_internal[cls_key] = iname

        # Lookup table: internalName -> material dict
        by_internal: Dict[str, dict] = {}
        for k, v in mats_raw.items():
            inner = v.get("internalName")
            if inner:
                by_internal[inner] = v

        out_materials: Dict[str, dict] = {}
        bundle_files: Dict[str, bytes] = {}  # zip_relpath -> bytes (deduped)
        our_terrain_dir = f"/levels/{level_name}/art/terrains"

        for cls_key, iname in wanted_internal.items():
            tmpl = by_internal.get(iname)
            if tmpl is None:
                continue
            mat = dict(tmpl)  # shallow copy

            # Use a unique internalName so we never collide with a
            # currently-loaded vanilla level material of the same name.
            new_internal = f"MapNG_{iname}"
            mat["internalName"] = new_internal
            # Use a stable UUID derived from the level name + class so
            # regenerating the same level gives the same persistentId.
            pid = str(uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"mapng:terrain:{level_name}:{new_internal}",
            ))
            mat["persistentId"] = pid

            # Rewrite every texture path
            for fk in _TEX_FIELDS:
                tex_path = mat.get(fk)
                if not isinstance(tex_path, str):
                    continue
                # Try to resolve via redirect
                target = (redirects.get(tex_path) or
                          redirects.get(tex_path.lower()))
                if target:
                    # Redirect points to /assets/materials/terrain/...
                    # which is in BeamNG's central terrain.zip (always
                    # mounted in VFS). Use it directly, no bundling.
                    mat[fk] = target
                    continue

                # Not redirected — must be a real .png in Industrial.zip.
                # Find the actual zip entry (case-insensitive search).
                # Industrial.zip uses 'Industrial' (capital) but the
                # materials.json uses 'industrial' (lowercase) in refs.
                src_logical = tex_path.lstrip("/")  # 'levels/industrial/art/...'
                src_member = None
                low = src_logical.lower()
                for member in zf.namelist():
                    if member.lower() == low:
                        src_member = member
                        break
                if src_member is None:
                    # Texture missing — drop the field rather than ship
                    # a broken reference
                    mat.pop(fk, None)
                    continue
                # Bundle the bytes into our mod at our terrain folder
                fname = src_logical.rsplit("/", 1)[-1]
                bundle_relpath = f"levels/{level_name}/art/terrains/{fname}"
                if bundle_relpath not in bundle_files:
                    bundle_files[bundle_relpath] = zf.read(src_member)
                mat[fk] = f"{our_terrain_dir}/{fname}"

            # Dict key uses the same Name-UUID pattern as vanilla
            out_materials[f"{new_internal}-{pid}"] = mat

        files = [(p, b) for p, b in bundle_files.items()]
        return out_materials, files


def class_to_internal_name(level_name: str, cls_key: str) -> str | None:
    """Return the prefixed internalName our vanilla pack uses for this class.

    Used by the .ter writer to put the right material name in the binary.
    """
    iname = CLASS_TO_INDUSTRIAL.get(cls_key)
    return f"MapNG_{iname}" if iname else None

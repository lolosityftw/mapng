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


def find_terrain_assets_zip(industrial_zip: Path) -> Path | None:
    """Locate the central terrain.zip relative to an Industrial.zip path.

    Industrial.zip is at .../content/levels/Industrial.zip
    terrain.zip is at  .../content/assets/materials/terrain.zip
    """
    base = industrial_zip.parent.parent  # .../content
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
) -> Tuple[Dict[str, dict], List[Tuple[str, bytes]]] | None:
    """Build a vanilla terrain material pack for the given list of class keys.

    Bundles ALL texture files into our mod (both Industrial-local PNGs and
    redirected /assets/... PNGs from terrain.zip). Empirically the redirect
    paths don't resolve from a mod context, so we copy everything in.

    side_m: terrain side length in metres. Used to scale `diffuseSize` so
    the base color texture wraps once across the whole map instead of the
    Industrial default (1024m/wrap) which tiles visibly on larger maps.
    detailSize (2m) and macroSize (80m) are kept small — they tile so
    frequently they read as noise rather than visible repetition.
    """
    industrial_zip = industrial_zip or find_industrial_zip()
    if industrial_zip is None:
        return None
    terrain_zip = find_terrain_assets_zip(industrial_zip)

    try:
        izf = zipfile.ZipFile(industrial_zip, "r")
    except (zipfile.BadZipFile, OSError):
        return None
    tzf = None
    if terrain_zip is not None:
        try:
            tzf = zipfile.ZipFile(terrain_zip, "r")
        except (zipfile.BadZipFile, OSError):
            tzf = None

    try:
        try:
            mats_raw = json.loads(izf.read(
                "levels/Industrial/art/terrains/main.materials.json"
            ))
        except KeyError:
            return None

        redirects = _build_redirect_map(izf)

        wanted_internal: Dict[str, str] = {}
        for cls_key in classes_used:
            iname = CLASS_TO_INDUSTRIAL.get(cls_key)
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
            mat["diffuseSize"] = int(side_m)

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

            # CRITICAL: Industrial's `t_terrain_base_b.png` is a RENDERED
            # image of Industrial's actual level (with race track, buildings,
            # etc.) — NOT a tileable terrain texture. Reusing it on our map
            # shows Industrial's race track tiled. Override baseColorBaseTex
            # AFTER the resolve loop so the bundled (tiled-pattern) industrial
            # base color is dropped, and use OUR composite terrain.png
            # instead (our OSM-derived satellite-like imagery).
            # The other Base channels (normalBase/roughnessBase/etc.) are
            # neutral fillers that work for any terrain — leave them.
            mat["baseColorBaseTex"] = f"{our_terrain_dir}/terrain.png"
            mat["baseColorBaseTexSize"] = int(side_m)

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


def class_to_internal_name(level_name: str, cls_key: str) -> str | None:
    """Return the prefixed internalName our vanilla pack uses for this class.

    Used by the .ter writer to put the right material name in the binary.
    """
    iname = CLASS_TO_INDUSTRIAL.get(cls_key)
    return f"MapNG_{iname}" if iname else None

"""Vanilla BeamNG terrain pack — port of nikkiluzader/mapng's approach.

Key insight from the working reference (services/osmTerrainMaterials.js):

  1. Don't bundle vanilla textures into our mod. Reference them via their
     real VFS paths (`/levels/east_coast_usa/art/terrains/...`). BeamNG
     mounts every vanilla level zip at startup so those paths resolve.

  2. Override ONLY the base slots in each cloned vanilla material — point
     baseColorBaseTex at our satellite composite, point base-AO/normal/
     roughness/height at small SHARED NEUTRAL textures we generate.
     Keep the detail/macro slots pointing at the original vanilla paths
     so the close-range PBR look stays intact.

  3. The TerrainMaterialTextureSet's baseTexSize must match the actual
     pixel dimensions of the satellite composite (typically 2048).

  4. Material dict keys follow vanilla format: `{InternalName}-{uuid}`,
     and `name` is set to the same value.

REFERENCE_MATERIALS templates below are clones of vanilla TerrainMaterials
from East Coast USA / GridMap v2 / Utah — picked for the rural look that
fits NI countryside.
"""
from __future__ import annotations

import io
import uuid
from pathlib import Path
from typing import Dict, List, Tuple

import numpy as np
from PIL import Image


# ----------------------------------------------------------------------------
# Reference templates — mirrored from nikkiluzader/mapng's REFERENCE_MATERIALS
# (services/osmTerrainMaterials.js). Each template has the full PBR field
# set; we'll override the base slots when cloning.
# ----------------------------------------------------------------------------

_TEMPLATE_GRASS = {
    "class": "TerrainMaterial",
    "annotation": "GRASS",
    "aoBaseTex":   "/levels/east_coast_usa/art/terrains/t_terrain_base_ao.png",
    "aoBaseTexSize": 2048,
    "aoDetailTex": "/levels/east_coast_usa/art/terrains/t_grass1_ao.png",
    "aoMacroTex":  "/levels/east_coast_usa/art/terrains/t_macro_grass_ao.png",
    "aoMacroTexSize": 100,
    "baseColorBaseTex":     "/levels/east_coast_usa/art/terrains/t_terrain_base_b.png",
    "baseColorBaseTexSize": 2048,
    "baseColorDetailStrength": [0.32, 0.0],
    "baseColorDetailTex":   "/levels/east_coast_usa/art/terrains/t_grass1_b.png",
    "baseColorMacroStrength":[0.10, 0.40],
    "baseColorMacroTex":    "/levels/east_coast_usa/art/terrains/t_macro_grass_b.png",
    "baseColorMacroTexSize": 100,
    "detailDistAtten":  [0.0, 0.9],
    "detailDistances":  [0, 0, 30, 60],
    "groundmodelName":  "GRASS",
    "heightBaseTex":    "/levels/east_coast_usa/art/terrains/t_terrain_base_h.png",
    "heightBaseTexSize":2048,
    "heightDetailTex":  "/levels/east_coast_usa/art/terrains/t_grass1_h.png",
    "heightMacroTex":   "/levels/east_coast_usa/art/terrains/t_macro_grass_h.png",
    "heightMacroTexSize": 100,
    "macroDistAtten":   [0.35, 0.0],
    "macroDistances":   [0, 0, 400, 8000],
    "normalBaseTex":    "/levels/east_coast_usa/art/terrains/t_terrain_base_nm.png",
    "normalBaseTexSize":2048,
    "normalDetailStrength": [0.6, 0.0],
    "normalDetailTex":  "/levels/east_coast_usa/art/terrains/t_grass1_nm.png",
    "normalMacroStrength": [0.4, 0.6],
    "normalMacroTex":   "/levels/east_coast_usa/art/terrains/t_macro_grass_nm.png",
    "normalMacroTexSize": 100,
    "roughnessBaseTex": "/levels/east_coast_usa/art/terrains/t_terrain_base_r.png",
    "roughnessBaseTexSize": 2048,
    "roughnessDetailStrength": [0.9, 0.0],
    "roughnessDetailTex":   "/levels/east_coast_usa/art/terrains/t_grass1_r.png",
    "roughnessMacroStrength":[0.2, 0.5],
    "roughnessMacroTex":   "/levels/east_coast_usa/art/terrains/t_macro_grass_r.png",
    "roughnessMacroTexSize": 100,
}

_TEMPLATE_DIRT = {
    "class": "TerrainMaterial",
    "aoBaseTex":   "/levels/gridmap_v2/art/terrains/t_terrain_base_ao.png",
    "aoBaseTexSize": 2048,
    "aoDetailTex": "/levels/gridmap_v2/art/terrains/t_dirt_loose_ao.png",
    "aoMacroTex":  "/levels/gridmap_v2/art/terrains/t_macro_rocky_ao.png",
    "baseColorBaseTex":     "/levels/gridmap_v2/art/terrains/t_terrain_base_b.png",
    "baseColorBaseTexSize": 2048,
    "baseColorDetailStrength": [0.25, 0.25],
    "baseColorDetailTex":   "/levels/gridmap_v2/art/terrains/t_dirt_loose_b.png",
    "baseColorMacroStrength":[0.10, 0.20],
    "baseColorMacroTex":    "/levels/gridmap_v2/art/terrains/t_macro_rocky_b.png",
    "detailSize": 2,
    "detailStrength": 0.5,
    "diffuseSize": 50,
    "groundmodelName": "DIRT",
    "heightBaseTex":    "/levels/gridmap_v2/art/terrains/t_terrain_base_h.png",
    "heightBaseTexSize":2048,
    "heightDetailTex":  "/levels/gridmap_v2/art/terrains/t_dirt_loose_h.png",
    "heightMacroTex":   "/levels/gridmap_v2/art/terrains/t_macro_rocky_h.png",
    "macroDistance": 1000,
    "macroDistances":[0, 10, 100, 3000],
    "macroSize": 40,
    "macroStrength": 0.5,
    "normalBaseTex":    "/levels/gridmap_v2/art/terrains/t_terrain_base_nm.png",
    "normalBaseTexSize":2048,
    "normalDetailStrength": [0.7, 0.15],
    "normalDetailTex":  "/levels/gridmap_v2/art/terrains/t_dirt_loose_nm.png",
    "normalMacroStrength": [0.30, 0.40],
    "normalMacroTex":   "/levels/gridmap_v2/art/terrains/t_macro_rocky_nm.png",
    "roughnessBaseTex": "/levels/gridmap_v2/art/terrains/t_terrain_base_r.png",
    "roughnessBaseTexSize": 2048,
    "roughnessDetailStrength": [0.3, 0.3],
    "roughnessDetailTex":   "/levels/gridmap_v2/art/terrains/t_dirt_loose_r.png",
    "roughnessMacroStrength":[0.20, 0.70],
    "roughnessMacroTex":   "/levels/gridmap_v2/art/terrains/t_macro_rocky_r.png",
}

_TEMPLATE_BEACHSAND = {
    "class": "TerrainMaterial",
    "annotation": "SAND",
    "aoBaseTex":   "/levels/gridmap_v2/art/terrains/t_terrain_base_ao.png",
    "aoBaseTexSize": 2048,
    "aoDetailTex": "/levels/gridmap_v2/art/terrains/t_beachsand_ao.png",
    "aoMacroTex":  "/levels/gridmap_v2/art/terrains/t_macro_clumpy_ao.png",
    "baseColorBaseTex":     "/levels/gridmap_v2/art/terrains/t_terrain_base_b.png",
    "baseColorBaseTexSize": 2048,
    "baseColorDetailStrength": [0.25, 0.25],
    "baseColorDetailTex":   "/levels/gridmap_v2/art/terrains/t_beachsand_b.png",
    "baseColorMacroStrength":[0.05, 0.10],
    "baseColorMacroTex":    "/levels/gridmap_v2/art/terrains/t_macro_clumpy_b.png",
    "detailSize": 2, "detailStrength": 0.5, "diffuseSize": 50,
    "groundmodelName": "SAND",
    "heightBaseTex":    "/levels/gridmap_v2/art/terrains/t_terrain_base_h.png",
    "heightBaseTexSize":2048,
    "heightDetailTex":  "/levels/gridmap_v2/art/terrains/t_beachsand_h.png",
    "heightMacroTex":   "/levels/gridmap_v2/art/terrains/t_macro_clumpy_h.png",
    "macroDistance": 1000, "macroDistances":[0, 10, 100, 3000],
    "macroSize": 40, "macroStrength": 0.5,
    "normalBaseTex":    "/levels/gridmap_v2/art/terrains/t_terrain_base_nm.png",
    "normalBaseTexSize":2048,
    "normalDetailStrength": [0.7, 0.15],
    "normalDetailTex":  "/levels/gridmap_v2/art/terrains/t_beachsand_nm.png",
    "normalMacroStrength": [0.25, 0.25],
    "normalMacroTex":   "/levels/gridmap_v2/art/terrains/t_macro_clumpy_nm.png",
    "roughnessBaseTex": "/levels/gridmap_v2/art/terrains/t_terrain_base_r.png",
    "roughnessBaseTexSize": 2048,
}

_TEMPLATE_ROCK = {
    "class": "TerrainMaterial",
    "annotation": "ROCK",
    "aoBaseTex":   "/levels/east_coast_usa/art/terrains/t_terrain_base_ao.png",
    "aoBaseTexSize": 2048,
    "aoDetailTex": "/levels/east_coast_usa/art/terrains/t_rock1_ao.png",
    "baseColorBaseTex":     "/levels/east_coast_usa/art/terrains/t_terrain_base_b.png",
    "baseColorBaseTexSize": 2048,
    "baseColorDetailStrength": [0.5, 0.0],
    "baseColorDetailTex":   "/levels/east_coast_usa/art/terrains/t_rock1_b.png",
    "groundmodelName": "ROCK",
    "heightBaseTex":    "/levels/east_coast_usa/art/terrains/t_terrain_base_h.png",
    "heightBaseTexSize":2048,
    "heightDetailTex":  "/levels/east_coast_usa/art/terrains/t_rock1_h.png",
    "normalBaseTex":    "/levels/east_coast_usa/art/terrains/t_terrain_base_nm.png",
    "normalBaseTexSize":2048,
    "normalDetailStrength": [0.6, 0.0],
    "normalDetailTex":  "/levels/east_coast_usa/art/terrains/t_rock1_nm.png",
    "roughnessBaseTex": "/levels/east_coast_usa/art/terrains/t_terrain_base_r.png",
    "roughnessBaseTexSize": 2048,
    "roughnessDetailTex":   "/levels/east_coast_usa/art/terrains/t_rock1_r.png",
}

_TEMPLATE_ASPHALT = {
    "class": "TerrainMaterial",
    "annotation": "ASPHALT",
    "aoBaseTex":   "/levels/east_coast_usa/art/terrains/t_terrain_base_ao.png",
    "aoBaseTexSize": 2048,
    "aoDetailTex": "/levels/east_coast_usa/art/terrains/t_asphalt_ao.png",
    "baseColorBaseTex":     "/levels/east_coast_usa/art/terrains/t_terrain_base02_b.png",
    "baseColorBaseTexSize": 2048,
    "baseColorDetailStrength": [0.3, 0.1],
    "baseColorDetailTex":   "/levels/east_coast_usa/art/terrains/t_asphalt_b.png",
    "diffuseSize": 50,
    "groundmodelName":  "ASPHALT",
    "heightBaseTex":    "/levels/east_coast_usa/art/terrains/t_terrain_base_h.png",
    "heightBaseTexSize":2048,
    "heightDetailTex":  "/levels/east_coast_usa/art/terrains/t_asphalt_h.png",
    "normalBaseTex":    "/levels/east_coast_usa/art/terrains/t_terrain_base_nm.png",
    "normalBaseTexSize":2048,
    "normalDetailStrength": [0.6, 0.0],
    "normalDetailTex":  "/levels/east_coast_usa/art/terrains/t_asphalt_nm.png",
    "roughnessBaseTex": "/levels/east_coast_usa/art/terrains/t_terrain_base_r.png",
    "roughnessBaseTexSize": 2048,
    "roughnessDetailTex":   "/levels/east_coast_usa/art/terrains/t_asphalt_r.png",
}

_TEMPLATE_GRAVEL = {
    "class": "TerrainMaterial",
    "annotation": "GRAVEL",
    "aoBaseTex":   "/levels/Utah/art/terrains/t_terrain_base_ao.png",
    "aoBaseTexSize": 2048,
    "aoDetailTex": "/levels/Utah/art/terrains/t_dirt_rocky_ao.png",
    "baseColorBaseTex":     "/levels/Utah/art/terrains/t_terrain_base_b.png",
    "baseColorBaseTexSize": 2048,
    "baseColorDetailStrength": [0.4, 0.1],
    "baseColorDetailTex":   "/levels/Utah/art/terrains/t_dirt_rocky_b.png",
    "diffuseSize": 50,
    "groundmodelName":  "GRAVEL",
    "heightBaseTex":    "/levels/Utah/art/terrains/t_terrain_base_h.png",
    "heightBaseTexSize":2048,
    "heightDetailTex":  "/levels/Utah/art/terrains/t_dirt_rocky_h.png",
    "normalBaseTex":    "/levels/Utah/art/terrains/t_terrain_base_nm.png",
    "normalBaseTexSize":2048,
    "normalDetailTex":  "/levels/Utah/art/terrains/t_dirt_rocky_nm.png",
    "roughnessBaseTex": "/levels/Utah/art/terrains/t_terrain_base_r.png",
    "roughnessBaseTexSize": 2048,
    "roughnessDetailTex":   "/levels/Utah/art/terrains/t_dirt_rocky_r.png",
}

_TEMPLATE_CONCRETE = {
    "class": "TerrainMaterial",
    "annotation": "CONCRETE",
    "aoBaseTex":   "/levels/gridmap_v2/art/terrains/t_terrain_base_ao.png",
    "aoBaseTexSize": 2048,
    "aoDetailTex": "/levels/gridmap_v2/art/terrains/t_concrete_damaged_ao.png",
    "baseColorBaseTex":     "/levels/gridmap_v2/art/terrains/t_terrain_base_b.png",
    "baseColorBaseTexSize": 2048,
    "baseColorDetailStrength": [0.3, 0.1],
    "baseColorDetailTex":   "/levels/gridmap_v2/art/terrains/t_concrete_damaged_b.png",
    "diffuseSize": 50,
    "groundmodelName":  "CONCRETE",
    "heightBaseTex":    "/levels/gridmap_v2/art/terrains/t_terrain_base_h.png",
    "heightBaseTexSize":2048,
    "heightDetailTex":  "/levels/gridmap_v2/art/terrains/t_concrete_damaged_h.png",
    "normalBaseTex":    "/levels/gridmap_v2/art/terrains/t_terrain_base_nm.png",
    "normalBaseTexSize":2048,
    "normalDetailTex":  "/levels/gridmap_v2/art/terrains/t_concrete_damaged_nm.png",
    "roughnessBaseTex": "/levels/gridmap_v2/art/terrains/t_terrain_base_r.png",
    "roughnessBaseTexSize": 2048,
    "roughnessDetailTex":   "/levels/gridmap_v2/art/terrains/t_concrete_damaged_r.png",
}


# Mapping: our class key → (internalName, template). DefaultMaterial added separately.
_CLASS_TO_TEMPLATE: Dict[str, Tuple[str, Dict]] = {
    "asphalt":  ("asphalt",   _TEMPLATE_ASPHALT),
    "concrete": ("Concrete",  _TEMPLATE_CONCRETE),
    "lawn":     ("Grass",     _TEMPLATE_GRASS),
    "pasture":  ("Grass",     _TEMPLATE_GRASS),
    "earth":    ("Dirt",      _TEMPLATE_DIRT),
    "gravel":   ("GRAVEL",    _TEMPLATE_GRAVEL),
    "water":    ("BeachSand", _TEMPLATE_BEACHSAND),
    "forest":   ("Grass",     _TEMPLATE_GRASS),  # forest_floor not stable enough cross-level
}


# ----------------------------------------------------------------------------
# Shared neutral textures (small, written once per export)
# ----------------------------------------------------------------------------

def _white_ao_png(size: int) -> bytes:
    """Pure-white ambient occlusion (no occlusion baked in)."""
    img = Image.new("L", (size, size), 255)
    buf = io.BytesIO(); img.save(buf, format="PNG"); return buf.getvalue()


def _flat_normal_png(size: int) -> bytes:
    """Flat normal map: RGB(128, 128, 255) — perfect 'no bump' surface."""
    arr = np.full((size, size, 3), [128, 128, 255], dtype=np.uint8)
    img = Image.fromarray(arr)
    buf = io.BytesIO(); img.save(buf, format="PNG"); return buf.getvalue()


def _neutral_roughness_png(size: int, value: int = 180) -> bytes:
    """Mid-roughness greyscale (180 = slightly glossy, default for terrain)."""
    img = Image.new("L", (size, size), value)
    buf = io.BytesIO(); img.save(buf, format="PNG"); return buf.getvalue()


# ----------------------------------------------------------------------------
# Build terrain pack
# ----------------------------------------------------------------------------

def _stable_uuid(seed: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"mapng:{seed}"))


def texture_set_name(level_name: str) -> str:
    """The TerrainMaterialTextureSet name — referenced by TerrainBlock."""
    return f"{level_name}TerrainMaterialTextureSet"


def class_to_internal_name(level_name: str, cls_key: str) -> str | None:
    """Match the prefixed internalName our pack uses for a class key.

    The pack uses semantic names ("Grass", "Dirt", etc.) so multiple
    classes can map to the same TerrainMaterial. The .ter binary stores
    one entry per LAYER (one per splat.layers item), which can repeat
    the same internalName.
    """
    entry = _CLASS_TO_TEMPLATE.get(cls_key)
    if entry is None:
        return None
    return entry[0]


def build_vanilla_terrain_pack(
    level_name: str,
    classes_used: List[str],
    industrial_zip: Path | None = None,  # legacy, unused
    side_m: float = 1024.0,
    source: str = "italy",                 # legacy, unused (we hardcode templates)
) -> Tuple[Dict[str, dict], List[Tuple[str, bytes]]] | None:
    """Build the terrain materials.json + companion shared textures.

    Mirrors nikkiluzader/mapng's `buildTerrainMaterials` exactly:
      - 1 TerrainMaterialTextureSet
      - 1 DefaultMaterial (semantic name, satellite base, neutral other channels)
      - N cloned TerrainMaterials (one per unique class), with base slots
        overridden to satellite + shared neutrals; detail/macro slots keep
        their vanilla VFS paths so PBR detail layers work
      - 6 small shared neutral PNGs to bundle (white AO, flat normal, mid roughness;
        at base size and detail size = 1024)

    Returns: (materialDefs dict, files-to-bundle list of (relpath, bytes))
    """
    BASE_SIZE = 2048   # composite (terrain.png) is 2048×2048 PIXELS
    DETAIL_SIZE = 1024 # detail/macro neutral fallbacks — pixel size

    # diffuseSize is in WORLD METRES — controls how the base texture is
    # stretched across the terrain. Setting it = side_m makes the satellite
    # composite cover the whole map exactly once (no visible tiling).
    DIFFUSE_SIZE_M = max(64.0, float(side_m))

    materials: Dict[str, dict] = {}
    files: List[Tuple[str, bytes]] = []

    # Shared neutral textures — only 6 small PNGs total (much smaller than
    # bundling Industrial's 75 textures)
    files.extend([
        (f"levels/{level_name}/art/terrains/shared_ao.png",    _white_ao_png(BASE_SIZE)),
        (f"levels/{level_name}/art/terrains/shared_nm.png",    _flat_normal_png(BASE_SIZE)),
        (f"levels/{level_name}/art/terrains/shared_r.png",     _neutral_roughness_png(BASE_SIZE)),
        (f"levels/{level_name}/art/terrains/shared_ao_sm.png", _white_ao_png(DETAIL_SIZE)),
        (f"levels/{level_name}/art/terrains/shared_nm_sm.png", _flat_normal_png(DETAIL_SIZE)),
        (f"levels/{level_name}/art/terrains/shared_r_sm.png",  _neutral_roughness_png(DETAIL_SIZE)),
    ])

    # ── TerrainMaterialTextureSet ────────────────────────────────────────────
    ts_name = texture_set_name(level_name)
    materials[ts_name] = {
        "name": ts_name,
        "class": "TerrainMaterialTextureSet",
        "persistentId": _stable_uuid(f"textureset:{level_name}"),
        "baseTexSize":   [BASE_SIZE, BASE_SIZE],
        "detailTexSize": [DETAIL_SIZE, DETAIL_SIZE],
        "macroTexSize":  [DETAIL_SIZE, DETAIL_SIZE],
    }

    # Helper: paths inside our level for shared neutrals + satellite
    p = lambda f: f"/levels/{level_name}/art/terrains/{f}"
    satellite = p("terrain.png")

    def neutral_base_overrides() -> dict:
        return {
            "aoBaseTex":         p("shared_ao.png"),    "aoBaseTexSize":        BASE_SIZE,
            "normalBaseTex":     p("shared_nm.png"),    "normalBaseTexSize":    BASE_SIZE,
            "roughnessBaseTex":  p("shared_r.png"),     "roughnessBaseTexSize": BASE_SIZE,
            "heightBaseTex":     p("shared_r.png"),     "heightBaseTexSize":    BASE_SIZE,
        }

    # ── DefaultMaterial (semantic name, no detail layer) ────────────────────
    default_uuid = _stable_uuid(f"DefaultMaterial:{level_name}")
    default_key = f"DefaultMaterial-{default_uuid}"
    materials[default_key] = {
        "name": default_key,
        "class": "TerrainMaterial",
        "persistentId": default_uuid,
        "internalName": "DefaultMaterial",
        "groundmodelName": "GROUNDMODEL_ASPHALT1",
        "baseColorBaseTex": satellite,
        "baseColorBaseTexSize": BASE_SIZE,
        "diffuseSize": DIFFUSE_SIZE_M,
        # Neutral overrides for detail/macro/etc. — DefaultMaterial is
        # rendered for fill areas where we don't have a specific class
        "baseColorDetailTex":   p("shared_r_sm.png"), "baseColorDetailStrength": [0, 0],
        "baseColorMacroTex":    p("shared_r_sm.png"), "baseColorMacroStrength":  [0, 0],
        "normalDetailTex":      p("shared_nm_sm.png"), "normalDetailStrength":   [0, 0],
        "normalMacroTex":       p("shared_nm_sm.png"), "normalMacroStrength":    [0, 0],
        "roughnessDetailTex":   p("shared_r_sm.png"), "roughnessDetailStrength": [0, 0],
        "roughnessMacroTex":    p("shared_r_sm.png"), "roughnessMacroStrength":  [0, 0],
        "aoDetailTex":          p("shared_ao_sm.png"),
        "aoMacroTex":           p("shared_ao_sm.png"),
        "heightDetailTex":      p("shared_r_sm.png"),
        "heightMacroTex":       p("shared_r_sm.png"),
        **neutral_base_overrides(),
    }

    # ── Cloned vanilla materials (one per unique class) ─────────────────────
    seen_internals: set[str] = set()
    for cls_key in classes_used:
        entry = _CLASS_TO_TEMPLATE.get(cls_key)
        if entry is None:
            continue
        internal_name, template = entry
        if internal_name in seen_internals:
            continue
        seen_internals.add(internal_name)

        muuid = _stable_uuid(f"{internal_name}:{level_name}")
        mkey = f"{internal_name}-{muuid}"
        # Deep-copy the template (shallow + fix lists)
        mat = {k: (list(v) if isinstance(v, list) else v) for k, v in template.items()}
        mat["name"] = mkey
        mat["persistentId"] = muuid
        mat["internalName"] = internal_name
        # Override BASE slots to point at our composite + shared neutrals
        mat["baseColorBaseTex"] = satellite
        mat["baseColorBaseTexSize"] = BASE_SIZE
        mat["diffuseSize"] = DIFFUSE_SIZE_M
        # Force consistent close-range detail values so detail layer
        # actually shows up close. Without explicit detailSize, BeamNG's
        # default makes detail invisible on some materials.
        mat.setdefault("detailSize", 2)
        mat.setdefault("detailStrength", 0.6)
        mat.setdefault("macroSize", 80)
        mat.setdefault("macroStrength", 0.15)
        mat.update(neutral_base_overrides())
        materials[mkey] = mat

    return materials, files

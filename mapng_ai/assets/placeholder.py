"""PlaceholderProvider — generates per-type unit DAE files (1×1×1 m).

Uses raw COLLADA XML with unique MapNG_* material names to prevent BeamNG from
resolving generic names like 'material_0' against textures from other installed
levels (which causes kanji/Asian DLC textures to appear on generated buildings).
"""
from __future__ import annotations

import math
from pathlib import Path
from typing import Sequence

from mapng_ai import config
from mapng_ai.assets.base import AssetProvider, BuildingAsset


# Spec §5.2 palette
_TYPE_COLORS: dict[str, str] = {
    "residential": "#E8D5B7",
    "house":       "#E8D5B7",
    "detached":    "#E8D5B7",
    "semi":        "#E8D5B7",
    "commercial":  "#7A8DA0",
    "office":      "#7A8DA0",
    "industrial":  "#6B6B6B",
    "warehouse":   "#6B6B6B",
    "retail":      "#C49C7A",
    "shop":        "#C49C7A",
    "apartment":   "#A8957C",
    "apartments":  "#A8957C",
    "garage":      "#8B7355",
    "shed":        "#8B7355",
    "barn":        "#8B7355",
    "default":     "#999999",
}

_ROOF_COLORS: dict[str, str] = {
    "residential": "#7A4F3B",
    "house":       "#7A4F3B",
    "detached":    "#7A4F3B",
    "semi":        "#7A4F3B",
    "commercial":  "#3F4954",
    "office":      "#3F4954",
    "industrial":  "#3F3F3F",
    "warehouse":   "#3F3F3F",
    "retail":      "#5F4636",
    "shop":        "#5F4636",
    "apartment":   "#544738",
    "apartments":  "#544738",
    "garage":      "#4D3F30",
    "shed":        "#4D3F30",
    "barn":        "#4D3F30",
    "default":     "#444444",
}

_FLAT_ROOF_TYPES: frozenset[str] = frozenset({
    "industrial", "warehouse", "garage", "shed", "barn",
    "commercial", "retail", "shop", "office",
})


def _hex_to_rgba(h: str, a: int = 255) -> list[int]:
    h = h.lstrip("#")
    return [int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), a]


def _hex_to_float3(h: str) -> tuple[float, float, float]:
    r, g, b, _ = _hex_to_rgba(h)
    return r / 255.0, g / 255.0, b / 255.0


# ---------------------------------------------------------------------------
# Raw COLLADA writer — avoids trimesh's generic material names
# ---------------------------------------------------------------------------

def _write_collada(
    path: Path,
    verts: Sequence[tuple[float, float, float]],
    faces: Sequence[tuple[int, int, int]],
    face_mat_ids: Sequence[int],
    materials: list[tuple[str, tuple[float, float, float]]],
) -> None:
    """Write a minimal COLLADA (.dae) file with per-face materials.

    materials: list of (mat_name, (r,g,b)) — names must be globally unique
    face_mat_ids: index into materials for each face
    """
    n_mats = len(materials)

    # Build per-material face lists
    mat_faces: list[list[tuple[int, int, int]]] = [[] for _ in range(n_mats)]
    for tri, mid in zip(faces, face_mat_ids):
        mat_faces[mid].append(tri)

    def _vf(x: float) -> str:
        return f"{x:.6f}"

    positions_str = " ".join(
        f"{_vf(v[0])} {_vf(v[1])} {_vf(v[2])}" for v in verts
    )

    # effects
    effects_xml = ""
    for name, (r, g, b) in materials:
        effects_xml += (
            f'  <effect id="{name}-effect">\n'
            f'    <profile_COMMON>\n'
            f'      <technique sid="common">\n'
            f'        <lambert>\n'
            f'          <diffuse><color>{_vf(r)} {_vf(g)} {_vf(b)} 1</color></diffuse>\n'
            f'        </lambert>\n'
            f'      </technique>\n'
            f'    </profile_COMMON>\n'
            f'  </effect>\n'
        )

    # materials
    mats_xml = ""
    for name, _ in materials:
        mats_xml += (
            f'  <material id="{name}" name="{name}">\n'
            f'    <instance_effect url="#{name}-effect"/>\n'
            f'  </material>\n'
        )

    n_verts = len(verts)
    geom_id = "mesh0"
    pos_src = f"{geom_id}-positions"

    # geometry — all faces in one mesh, per-material polylist
    polylists_xml = ""
    for mid, (name, _) in enumerate(materials):
        tris = mat_faces[mid]
        if not tris:
            continue
        count = len(tris)
        pdata = " ".join(f"{a} {b} {c}" for a, b, c in tris)
        polylists_xml += (
            f'      <triangles material="{name}" count="{count}">\n'
            f'        <input semantic="VERTEX" source="#mesh0-vertices" offset="0"/>\n'
            f'        <p>{pdata}</p>\n'
            f'      </triangles>\n'
        )

    # instance_material bindings
    bindings_xml = ""
    for name, _ in materials:
        if mat_faces[materials.index((name, materials[[m[0] for m in materials].index(name)][1]))]:
            bindings_xml += (
                f'          <instance_material symbol="{name}" target="#{name}"/>\n'
            )

    # Simpler binding generation
    bindings_xml = ""
    for mid, (name, _) in enumerate(materials):
        if mat_faces[mid]:
            bindings_xml += (
                f'          <instance_material symbol="{name}" target="#{name}"/>\n'
            )

    dae = f"""<?xml version="1.0" encoding="utf-8"?>
<COLLADA xmlns="http://www.collada.org/2005/11/COLLADASchema" version="1.4.1">
  <asset><up_axis>Z_UP</up_axis></asset>
  <library_effects>
{effects_xml}  </library_effects>
  <library_materials>
{mats_xml}  </library_materials>
  <library_geometries>
    <geometry id="{geom_id}" name="{geom_id}">
      <mesh>
        <source id="{pos_src}">
          <float_array id="{pos_src}-array" count="{n_verts * 3}">{positions_str}</float_array>
          <technique_common>
            <accessor source="#{pos_src}-array" count="{n_verts}" stride="3">
              <param name="X" type="float"/>
              <param name="Y" type="float"/>
              <param name="Z" type="float"/>
            </accessor>
          </technique_common>
        </source>
        <vertices id="{geom_id}-vertices">
          <input semantic="POSITION" source="#{pos_src}"/>
        </vertices>
{polylists_xml}      </mesh>
    </geometry>
  </library_geometries>
  <library_visual_scenes>
    <visual_scene id="Scene" name="Scene">
      <node id="Mesh" name="Mesh" type="NODE">
        <instance_geometry url="#{geom_id}">
          <bind_material><technique_common>
{bindings_xml}          </technique_common></bind_material>
        </instance_geometry>
      </node>
    </visual_scene>
  </library_visual_scenes>
  <scene><instance_visual_scene url="#Scene"/></scene>
</COLLADA>
"""
    path.write_text(dae, encoding="utf-8")


# ---------------------------------------------------------------------------
# Building geometry builders
# ---------------------------------------------------------------------------

def _pitched_box_collada(path: Path, wall_hex: str, roof_hex: str,
                          flat_roof: bool, mat_name: str) -> None:
    box_h = 1.0 if flat_roof else 0.7
    ridge_z = box_h if flat_roof else 1.0

    verts: list[tuple[float, float, float]] = [
        (-0.5, -0.5, 0),      # 0 SW base
        ( 0.5, -0.5, 0),      # 1 SE base
        ( 0.5,  0.5, 0),      # 2 NE base
        (-0.5,  0.5, 0),      # 3 NW base
        (-0.5, -0.5, box_h),  # 4 SW top
        ( 0.5, -0.5, box_h),  # 5 SE top
        ( 0.5,  0.5, box_h),  # 6 NE top
        (-0.5,  0.5, box_h),  # 7 NW top
        (-0.5,  0.0, ridge_z),# 8 W ridge
        ( 0.5,  0.0, ridge_z),# 9 E ridge
    ]

    WALL, ROOF = 0, 1
    faces: list[tuple[int, int, int]] = [
        (0, 2, 1), (0, 3, 2),        # base
        (0, 1, 5), (0, 5, 4),        # south wall
        (1, 2, 6), (1, 6, 5),        # east wall
        (2, 3, 7), (2, 7, 6),        # north wall
        (3, 0, 4), (3, 4, 7),        # west wall
        (4, 8, 7),                   # west gable
        (5, 6, 9),                   # east gable
        (4, 5, 9), (4, 9, 8),        # south roof slope
        (7, 9, 6), (7, 8, 9),        # north roof slope
    ]
    # gables are WALL colour, roof slopes are ROOF colour
    mids: list[int] = [
        WALL, WALL,
        WALL, WALL,
        WALL, WALL,
        WALL, WALL,
        WALL, WALL,
        WALL,        # west gable
        WALL,        # east gable
        ROOF, ROOF,  # south slope
        ROOF, ROOF,  # north slope
    ]

    wall_mat = f"{mat_name}_wall"
    roof_mat = f"{mat_name}_roof"
    materials = [
        (wall_mat, _hex_to_float3(wall_hex)),
        (roof_mat, _hex_to_float3(roof_hex)),
    ]
    _write_collada(path, verts, faces, mids, materials)


# ---------------------------------------------------------------------------
# Foliage geometry
# ---------------------------------------------------------------------------

def _slab_collada(path: Path, hex_color: str, mat_name: str,
                  cx: float = 0.0, cy: float = 0.0, cz: float = 0.5) -> None:
    """Unit box (1×1×1) centred at (cx, cy, cz)."""
    hx, hy, hz = 0.5, 0.5, 0.5
    verts: list[tuple[float, float, float]] = [
        (cx - hx, cy - hy, cz - hz),  # 0
        (cx + hx, cy - hy, cz - hz),  # 1
        (cx + hx, cy + hy, cz - hz),  # 2
        (cx - hx, cy + hy, cz - hz),  # 3
        (cx - hx, cy - hy, cz + hz),  # 4
        (cx + hx, cy - hy, cz + hz),  # 5
        (cx + hx, cy + hy, cz + hz),  # 6
        (cx - hx, cy + hy, cz + hz),  # 7
    ]
    faces: list[tuple[int, int, int]] = [
        (0, 2, 1), (0, 3, 2),  # bottom
        (4, 5, 6), (4, 6, 7),  # top
        (0, 1, 5), (0, 5, 4),  # front
        (1, 2, 6), (1, 6, 5),  # right
        (2, 3, 7), (2, 7, 6),  # back
        (3, 0, 4), (3, 4, 7),  # left
    ]
    mids = [0] * len(faces)
    _write_collada(path, verts, faces, mids,
                   [(mat_name, _hex_to_float3(hex_color))])


def _bush_collada(path: Path) -> None:
    """Low-poly bush — flattened octahedron (8 tris, 6 verts).

    Scale-by-instance friendly: unit shape is roughly 1×1×0.7 metres.
    Material 'MapNG_bush' is solid green; vanilla TSStatic wraps the
    embedded color.
    """
    # 6 verts for a flattened octahedron (wider than tall — bush-shape)
    verts: list[tuple[float, float, float]] = [
        ( 0.0,  0.0,  0.7),    # 0 top
        ( 0.5,  0.0,  0.35),   # 1 east
        ( 0.0,  0.5,  0.35),   # 2 north
        (-0.5,  0.0,  0.35),   # 3 west
        ( 0.0, -0.5,  0.35),   # 4 south
        ( 0.0,  0.0,  0.0),    # 5 bottom
    ]
    faces: list[tuple[int, int, int]] = [
        # Top half — apex to mid ring
        (0, 1, 2), (0, 2, 3), (0, 3, 4), (0, 4, 1),
        # Bottom half — mid ring to base
        (5, 2, 1), (5, 3, 2), (5, 4, 3), (5, 1, 4),
    ]
    mids = [0] * len(faces)
    _write_collada(path, verts, faces, mids,
                   [("MapNG_bush", _hex_to_float3("#3F5A28"))])


def _tree_collada(path: Path) -> None:
    """Cylinder trunk + cone canopy."""
    trunk_r = 0.05
    trunk_h = 0.3
    canopy_r = 0.35
    canopy_h = 0.7
    segs = 8

    verts: list[tuple[float, float, float]] = []
    faces: list[tuple[int, int, int]] = []
    mids: list[int] = []

    # trunk — ring at z=0, ring at z=trunk_h + caps
    base_centre = len(verts)
    verts.append((0, 0, 0))
    base_ring = len(verts)
    for i in range(segs):
        a = 2 * math.pi * i / segs
        verts.append((trunk_r * math.cos(a), trunk_r * math.sin(a), 0.0))
    top_ring = len(verts)
    for i in range(segs):
        a = 2 * math.pi * i / segs
        verts.append((trunk_r * math.cos(a), trunk_r * math.sin(a), trunk_h))
    top_centre = len(verts)
    verts.append((0, 0, trunk_h))

    TRUNK, CANOPY = 0, 1
    for i in range(segs):
        n = (i + 1) % segs
        # bottom cap
        faces.append((base_centre, base_ring + n, base_ring + i))
        mids.append(TRUNK)
        # side quad
        faces.append((base_ring + i, base_ring + n, top_ring + n))
        mids.append(TRUNK)
        faces.append((base_ring + i, top_ring + n, top_ring + i))
        mids.append(TRUNK)
        # top cap
        faces.append((top_centre, top_ring + i, top_ring + n))
        mids.append(TRUNK)

    # canopy cone — base ring + apex
    canopy_base_centre = len(verts)
    verts.append((0, 0, trunk_h))
    canopy_base_ring = len(verts)
    for i in range(segs):
        a = 2 * math.pi * i / segs
        verts.append((canopy_r * math.cos(a), canopy_r * math.sin(a), trunk_h))
    apex = len(verts)
    verts.append((0, 0, trunk_h + canopy_h))

    for i in range(segs):
        n = (i + 1) % segs
        # base cap
        faces.append((canopy_base_centre, canopy_base_ring + i, canopy_base_ring + n))
        mids.append(CANOPY)
        # side
        faces.append((canopy_base_ring + i, apex, canopy_base_ring + n))
        mids.append(CANOPY)

    _write_collada(
        path, verts, faces, mids,
        [
            ("MapNG_tree_trunk", _hex_to_float3("#5D4037")),
            ("MapNG_tree_canopy", _hex_to_float3("#2E7D32")),
        ],
    )


# ---------------------------------------------------------------------------
# Public write functions (cached)
# ---------------------------------------------------------------------------

def _cache_dir() -> Path:
    d = config.CACHE_DIR / "shapes"
    d.mkdir(parents=True, exist_ok=True)
    return d


_PITCHED_DIR_REL = "art/shapes/buildings"
_TREE_REL = "art/shapes/foliage/tree.dae"
_HEDGE_REL = "art/shapes/foliage/hedge.dae"
_WALL_REL = "art/shapes/foliage/wall.dae"
_FENCE_REL = "art/shapes/foliage/fence.dae"
_GATE_REL = "art/shapes/foliage/gate.dae"
_BUSH_REL = "art/shapes/foliage/bush.dae"
_SHED_REL = "art/shapes/buildings/shed.dae"


def _pitched_path_for(building_type: str) -> tuple[Path, str]:
    key = building_type if building_type in _TYPE_COLORS else "default"
    rel = f"{_PITCHED_DIR_REL}/building_{key}.dae"
    return _cache_dir() / f"building_{key}.dae", rel


def _is_mapng_dae(p: Path) -> bool:
    """Return True only if the file was written by our COLLADA writer."""
    if not p.exists():
        return False
    try:
        return b"MapNG_" in p.read_bytes()[:2048]
    except OSError:
        return False


def write_pitched_dae(building_type: str) -> tuple[Path, str]:
    cache_path, rel = _pitched_path_for(building_type)
    if _is_mapng_dae(cache_path):
        return cache_path, rel
    key = building_type if building_type in _TYPE_COLORS else "default"
    mat_name = f"MapNG_bld_{key}"
    _pitched_box_collada(
        cache_path,
        wall_hex=_TYPE_COLORS.get(key, _TYPE_COLORS["default"]),
        roof_hex=_ROOF_COLORS.get(key, _ROOF_COLORS["default"]),
        flat_roof=key in _FLAT_ROOF_TYPES,
        mat_name=mat_name,
    )
    return cache_path, rel


def write_tree_dae() -> tuple[Path, str]:
    cache_path = _cache_dir() / "tree.dae"
    if not _is_mapng_dae(cache_path):
        _tree_collada(cache_path)
    return cache_path, _TREE_REL


def write_bush_dae() -> tuple[Path, str]:
    """Low-poly placeholder bush — 8 tris, 6 verts.
    Cheap enough for hundreds of instances per map."""
    cache_path = _cache_dir() / "bush.dae"
    if not _is_mapng_dae(cache_path):
        _bush_collada(cache_path)
    return cache_path, _BUSH_REL


def write_hedge_dae() -> tuple[Path, str]:
    cache_path = _cache_dir() / "hedge.dae"
    if not _is_mapng_dae(cache_path):
        _slab_collada(cache_path, "#3F5A28", "MapNG_hedge")
    return cache_path, _HEDGE_REL


def write_wall_dae() -> tuple[Path, str]:
    cache_path = _cache_dir() / "wall.dae"
    if not _is_mapng_dae(cache_path):
        _slab_collada(cache_path, "#8A8479", "MapNG_wall")
    return cache_path, _WALL_REL


def write_fence_dae() -> tuple[Path, str]:
    cache_path = _cache_dir() / "fence.dae"
    if not _is_mapng_dae(cache_path):
        _slab_collada(cache_path, "#5C4A2C", "MapNG_fence")
    return cache_path, _FENCE_REL


def write_gate_dae() -> tuple[Path, str]:
    cache_path = _cache_dir() / "gate.dae"
    if not _is_mapng_dae(cache_path):
        _slab_collada(cache_path, "#6E5530", "MapNG_gate")
    return cache_path, _GATE_REL


def write_shed_dae() -> tuple[Path, str]:
    cache_path = _cache_dir() / "shed.dae"
    if not _is_mapng_dae(cache_path):
        _slab_collada(cache_path, "#7A7A75", "MapNG_shed")
    return cache_path, _SHED_REL


# ---------------------------------------------------------------------------
class PlaceholderProvider:
    name = "placeholder"

    def can_provide(self, asset_kind: str) -> bool:
        return asset_kind in ("building", "tree", "hedge")

    def get_building(
        self,
        footprint_m2: float,
        levels: int,
        building_type: str,
        seed: int,
    ) -> BuildingAsset:
        cache_path, rel = write_pitched_dae(building_type)
        aspect = 1.4
        width = (footprint_m2 / aspect) ** 0.5
        length = width * aspect
        height = max(3.0, levels * 3.0)
        color = _TYPE_COLORS.get(building_type, _TYPE_COLORS["default"])
        return BuildingAsset(
            shape_relpath=rel,
            natural_size_m=(length, width, height),
            color_hex=color,
            type_label=building_type,
        )

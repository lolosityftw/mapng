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
<!-- MapNGMesh:v5 -->
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
                          flat_roof: bool, mat_name: str,
                          storeys: int = 1) -> None:
    """Generate a placeholder building DAE with windows + chimney.

    Single 1×1×1 unit shape — TSStatic scales it per-instance to OSM
    footprint dimensions. `flat_roof=True` for industrial / apartments
    (no gable). `storeys` controls window-row count: 1 for cottages,
    2-3 for terraces, 4-5 for apartments.
    """
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

    WALL, ROOF, CHIMNEY, WINDOW, DOOR = 0, 1, 2, 3, 4
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

    # ---- Windows: one row per storey, 3 windows per long wall, 2 per short ----
    # Each window is a small quad inset OUTWARDS from the wall by a tiny
    # delta so it shows on top of the wall material. At scale 10x in
    # game the inset = 5cm — barely a bay window depth, looks fine.
    OUTSET = 0.005
    win_w = 0.10   # ~10% of building width per window
    win_h_per_storey = (box_h - 0.05) / max(1, storeys + 1)
    win_h = win_h_per_storey * 0.5

    def _add_window_panel(centre_x: float, centre_y: float, centre_z: float,
                          half_horiz: float, half_vert: float,
                          face_axis: str):
        """Add a window quad on a wall face. face_axis = '+x'/'-x'/'+y'/'-y'."""
        nonlocal verts, faces, mids
        # Compute the 4 corners in 3D depending on the wall orientation
        if face_axis == "-y":   # south wall, normal points -y
            cy = -0.5 - OUTSET
            corners = [
                (centre_x - half_horiz, cy, centre_z - half_vert),
                (centre_x + half_horiz, cy, centre_z - half_vert),
                (centre_x + half_horiz, cy, centre_z + half_vert),
                (centre_x - half_horiz, cy, centre_z + half_vert),
            ]
            wind = [(0, 1, 2), (0, 2, 3)]
        elif face_axis == "+y":   # north wall
            cy = 0.5 + OUTSET
            corners = [
                (centre_x + half_horiz, cy, centre_z - half_vert),
                (centre_x - half_horiz, cy, centre_z - half_vert),
                (centre_x - half_horiz, cy, centre_z + half_vert),
                (centre_x + half_horiz, cy, centre_z + half_vert),
            ]
            wind = [(0, 1, 2), (0, 2, 3)]
        elif face_axis == "+x":   # east wall
            cx = 0.5 + OUTSET
            corners = [
                (cx, centre_y - half_horiz, centre_z - half_vert),
                (cx, centre_y + half_horiz, centre_z - half_vert),
                (cx, centre_y + half_horiz, centre_z + half_vert),
                (cx, centre_y - half_horiz, centre_z + half_vert),
            ]
            wind = [(0, 1, 2), (0, 2, 3)]
        else:                      # -x  (west wall)
            cx = -0.5 - OUTSET
            corners = [
                (cx, centre_y + half_horiz, centre_z - half_vert),
                (cx, centre_y - half_horiz, centre_z - half_vert),
                (cx, centre_y - half_horiz, centre_z + half_vert),
                (cx, centre_y + half_horiz, centre_z + half_vert),
            ]
            wind = [(0, 1, 2), (0, 2, 3)]
        ci = len(verts)
        verts.extend(corners)
        for tri in wind:
            faces.append(tuple(ci + i for i in tri))
            mids.append(WINDOW)

    # Place window rows at fractions of box height
    for storey in range(storeys):
        # vertical centre of this storey's window band
        z_centre = ((storey + 0.5) / max(1, storeys)) * (box_h - 0.05) + 0.025

        # South / north walls — 3 windows each
        for col in (-0.30, 0.0, 0.30):
            _add_window_panel(col, 0, z_centre, win_w, win_h, "-y")
            _add_window_panel(col, 0, z_centre, win_w, win_h, "+y")

        # East / west walls — 2 windows each
        for col in (-0.20, 0.20):
            _add_window_panel(0, col, z_centre, win_w, win_h, "+x")
            _add_window_panel(0, col, z_centre, win_w, win_h, "-x")

    # ---- Front door (south wall, ground storey only) ----
    door_w, door_h = 0.05, 0.20
    door_z = door_h
    ci = len(verts)
    verts.extend([
        (-door_w, -0.5 - OUTSET, 0),
        ( door_w, -0.5 - OUTSET, 0),
        ( door_w, -0.5 - OUTSET, door_z),
        (-door_w, -0.5 - OUTSET, door_z),
    ])
    faces.append((ci + 0, ci + 1, ci + 2))
    faces.append((ci + 0, ci + 2, ci + 3))
    mids.extend([DOOR, DOOR])

    # ---- Chimney (only on pitched roofs) ----
    if not flat_roof:
        cw = 0.06
        cx = 0.30
        ch = 0.35
        ch_z0 = ridge_z * 0.85
        ch_z1 = ridge_z + ch
        ci = len(verts)
        verts.extend([
            (cx - cw, -cw, ch_z0),
            (cx + cw, -cw, ch_z0),
            (cx + cw, +cw, ch_z0),
            (cx - cw, +cw, ch_z0),
            (cx - cw, -cw, ch_z1),
            (cx + cw, -cw, ch_z1),
            (cx + cw, +cw, ch_z1),
            (cx - cw, +cw, ch_z1),
        ])
        ch_faces = [
            (ci+0, ci+1, ci+5), (ci+0, ci+5, ci+4),
            (ci+1, ci+2, ci+6), (ci+1, ci+6, ci+5),
            (ci+2, ci+3, ci+7), (ci+2, ci+7, ci+6),
            (ci+3, ci+0, ci+4), (ci+3, ci+4, ci+7),
            (ci+4, ci+5, ci+6), (ci+4, ci+6, ci+7),
        ]
        faces.extend(ch_faces)
        mids.extend([CHIMNEY] * len(ch_faces))

    wall_mat = f"{mat_name}_wall"
    roof_mat = f"{mat_name}_roof"
    chim_mat = f"{mat_name}_chimney"
    win_mat  = f"{mat_name}_window"
    door_mat = f"{mat_name}_door"
    materials = [
        (wall_mat, _hex_to_float3(wall_hex)),
        (roof_mat, _hex_to_float3(roof_hex)),
        (chim_mat, _hex_to_float3("#8B6F4E")),
        (win_mat,  _hex_to_float3("#2C3942")),  # dark glass / shadow
        (door_mat, _hex_to_float3("#3F2A1E")),  # dark wood
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


def _pole_collada(path: Path) -> None:
    """Telegraph / power pole — vertical cylinder + 1 crossbar at top.

    Unit 1×1×1: the pole stem is 0.04m radius, ~0.95m tall; the crossbar
    is 0.18m wide × 0.05m thick at z=0.85. TSStatic scales per-instance
    to actual world size (typically 9m tall poles → scale_z=9.0).
    """
    pole_r = 0.04
    pole_h = 0.95
    segs = 6   # hexagonal cylinder — cheap

    verts: list[tuple[float, float, float]] = []
    faces: list[tuple[int, int, int]] = []
    mids: list[int] = []
    WOOD, CROSS = 0, 1

    # Pole bottom centre
    base_centre = len(verts)
    verts.append((0, 0, 0))
    base_ring = len(verts)
    for i in range(segs):
        a = 2 * math.pi * i / segs
        verts.append((pole_r * math.cos(a), pole_r * math.sin(a), 0.0))
    top_ring = len(verts)
    for i in range(segs):
        a = 2 * math.pi * i / segs
        verts.append((pole_r * math.cos(a), pole_r * math.sin(a), pole_h))
    top_centre = len(verts)
    verts.append((0, 0, pole_h))

    for i in range(segs):
        n = (i + 1) % segs
        faces.append((base_centre, base_ring + n, base_ring + i)); mids.append(WOOD)
        faces.append((base_ring + i, base_ring + n, top_ring + n));  mids.append(WOOD)
        faces.append((base_ring + i, top_ring + n, top_ring + i));   mids.append(WOOD)
        faces.append((top_centre, top_ring + i, top_ring + n));      mids.append(WOOD)

    # Crossbar — small horizontal box at z=0.85
    cb_x = 0.09
    cb_y = 0.025
    cb_z0 = 0.85
    cb_z1 = 0.90
    ci = len(verts)
    verts.extend([
        (-cb_x, -cb_y, cb_z0), ( cb_x, -cb_y, cb_z0),
        ( cb_x,  cb_y, cb_z0), (-cb_x,  cb_y, cb_z0),
        (-cb_x, -cb_y, cb_z1), ( cb_x, -cb_y, cb_z1),
        ( cb_x,  cb_y, cb_z1), (-cb_x,  cb_y, cb_z1),
    ])
    cb_faces = [
        (ci+0, ci+1, ci+5), (ci+0, ci+5, ci+4),
        (ci+1, ci+2, ci+6), (ci+1, ci+6, ci+5),
        (ci+2, ci+3, ci+7), (ci+2, ci+7, ci+6),
        (ci+3, ci+0, ci+4), (ci+3, ci+4, ci+7),
        (ci+4, ci+5, ci+6), (ci+4, ci+6, ci+7),
    ]
    faces.extend(cb_faces)
    mids.extend([CROSS] * len(cb_faces))

    _write_collada(path, verts, faces, mids,
                   [("MapNG_pole_wood",  _hex_to_float3("#4A3520")),
                    ("MapNG_pole_cross", _hex_to_float3("#3A2810"))])


def _bush_collada(path: Path) -> None:
    """Low-poly bush — flattened double-stacked octahedron (12 tris, 7 verts).

    Two slightly-offset bumps give a more rounded silhouette than a
    single octahedron while staying cheap. Unit size is ~1×1×0.7 m.
    """
    verts: list[tuple[float, float, float]] = [
        ( 0.0,  0.0,  0.7),    # 0 apex
        ( 0.5,  0.0,  0.4),    # 1 east mid
        ( 0.0,  0.5,  0.4),    # 2 north mid
        (-0.5,  0.0,  0.4),    # 3 west mid
        ( 0.0, -0.5,  0.4),    # 4 south mid
        # Lower ring (creates the bush "skirt" so it doesn't look like
        # a single pointy octahedron but more like 2 stacked humps)
        ( 0.4,  0.4,  0.05),   # 5 NE base
        (-0.4, -0.4,  0.05),   # 6 SW base
    ]
    faces: list[tuple[int, int, int]] = [
        # Top — apex to mid ring
        (0, 1, 2), (0, 2, 3), (0, 3, 4), (0, 4, 1),
        # Lower hump connections
        (1, 5, 2), (2, 5, 0),  # NE
        (3, 6, 4), (4, 6, 0),  # SW
        # Base sealing
        (5, 1, 4), (5, 4, 6),
        (6, 3, 2), (6, 2, 5),
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

    # Canopy: TWO STACKED cones (lower wider darker, upper narrower lighter)
    # — gives much better visual depth than a single cone, still cheap (~64 tris).
    CANOPY_DARK = 1
    CANOPY_LIGHT = 2

    # Lower cone (darker, wider) — base at trunk_h, apex at 80% height
    lower_z0 = trunk_h
    lower_z1 = trunk_h + canopy_h * 0.85
    lower_r = canopy_r
    lower_base = len(verts)
    verts.append((0, 0, lower_z0))
    lower_ring = len(verts)
    for i in range(segs):
        a = 2 * math.pi * i / segs
        verts.append((lower_r * math.cos(a), lower_r * math.sin(a), lower_z0))
    lower_apex = len(verts)
    verts.append((0, 0, lower_z1))
    for i in range(segs):
        n = (i + 1) % segs
        faces.append((lower_base, lower_ring + i, lower_ring + n))
        mids.append(CANOPY_DARK)
        faces.append((lower_ring + i, lower_apex, lower_ring + n))
        mids.append(CANOPY_DARK)

    # Upper cone (lighter, narrower) — sits on top, smaller
    upper_z0 = trunk_h + canopy_h * 0.45
    upper_z1 = trunk_h + canopy_h * 1.05
    upper_r  = canopy_r * 0.62
    upper_base = len(verts)
    verts.append((0, 0, upper_z0))
    upper_ring = len(verts)
    for i in range(segs):
        a = 2 * math.pi * i / segs
        verts.append((upper_r * math.cos(a), upper_r * math.sin(a), upper_z0))
    upper_apex = len(verts)
    verts.append((0, 0, upper_z1))
    for i in range(segs):
        n = (i + 1) % segs
        faces.append((upper_base, upper_ring + i, upper_ring + n))
        mids.append(CANOPY_LIGHT)
        faces.append((upper_ring + i, upper_apex, upper_ring + n))
        mids.append(CANOPY_LIGHT)

    _write_collada(
        path, verts, faces, mids,
        [
            ("MapNG_tree_trunk",       _hex_to_float3("#5D4037")),
            ("MapNG_tree_canopy",      _hex_to_float3("#2E7D32")),   # darker lower
            ("MapNG_tree_canopy_top",  _hex_to_float3("#4F8B3D")),   # lighter upper
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
_POLE_REL = "art/shapes/infrastructure/pole.dae"
_SHED_REL = "art/shapes/buildings/shed.dae"


def _pitched_path_for(building_type: str) -> tuple[Path, str]:
    key = building_type if building_type in _TYPE_COLORS else "default"
    rel = f"{_PITCHED_DIR_REL}/building_{key}.dae"
    return _cache_dir() / f"building_{key}.dae", rel


# Bump this when the placeholder mesh writers change so cached DAEs
# get regenerated on next export. Look for this token in the file.
_MESH_VERSION_TAG = b"MapNGMesh:v5"


def _is_mapng_dae(p: Path) -> bool:
    """Return True only if the file was written by our CURRENT COLLADA writer.
    Also returns False for older versions (stale cache) so they get rebuilt."""
    if not p.exists():
        return False
    try:
        head = p.read_bytes()[:2048]
    except OSError:
        return False
    return b"MapNG_" in head and _MESH_VERSION_TAG in head


# Storey count per building type — drives the number of window rows.
# Used by `write_pitched_dae` so an apartment placeholder gets 3 rows,
# a cottage gets 1, etc.
_STOREYS_BY_TYPE: dict[str, int] = {
    "residential": 1,
    "house":       1,
    "detached":    1,
    "semi":        1,
    "garage":      1,
    "shed":        1,
    "barn":        1,
    "default":     1,
    "shop":        1,
    "retail":      1,
    "commercial":  2,
    "office":      2,
    "industrial":  1,   # flat roof, single tall storey
    "warehouse":   1,
    "apartment":   3,
    "apartments":  3,
}


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
        storeys=_STOREYS_BY_TYPE.get(key, 1),
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


def write_pole_dae() -> tuple[Path, str]:
    """Telegraph / power pole placeholder (~36 tris).
    Cheap enough for hundreds of poles per map."""
    cache_path = _cache_dir() / "pole.dae"
    if not _is_mapng_dae(cache_path):
        _pole_collada(cache_path)
    return cache_path, _POLE_REL


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

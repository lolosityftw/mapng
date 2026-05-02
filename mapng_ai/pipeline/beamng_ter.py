"""BeamNG terrain `.ter` (version 9) binary writer.

Format from BeamNG terrain.json `binaryFormat` declaration:

    version, size, heightMap, layerMap, layerTextureMap, materialNames

i.e.

    [u8 version=9]
    [u32 size]                                — square side length, LE
    [u16 heightMap × size²]                   — quantised heightmap, LE
                                                row 0 = SOUTH edge, x increases EAST
    [u8  layerMap × size²]                    — material layer index per pixel
    [u8  layerTextureMap × size²]             — texture variant per pixel (0 default)
    [u32 materialCount]                       — LE
    repeat materialCount times:
        [u8 nameLen] OR [u8 0xFF, u16 nameLen]
        [u8 × nameLen] UTF-8 name

Earlier versions of this writer omitted `layerTextureMap`. That left
BeamNG reading `materialCount` from the byte that should have been the
first pixel of the (missing) texture map — typically a small uint
representing 0..N material indices — and then trying to decode that
many UTF-8 strings from random terrain bytes. The result was a silent
parse failure → terrain block initialised with no surface.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np


def write_ter(
    heightmap_m: np.ndarray,
    out_path: Path,
    *,
    layer_map: np.ndarray | None = None,
    material_names: list[str] | None = None,
) -> tuple[float, float]:
    """Encode a square heightmap (rows = north→south, x = west→east) to .ter.

    Returns the (min_m, max_m) used for quantisation — needed so the engine can
    render at world scale (we pass `heightScale` separately in terrain.json).
    """
    h, w = heightmap_m.shape
    if h != w:
        raise ValueError(f".ter requires square terrain, got {h}×{w}")
    size = h
    if material_names is None:
        material_names = ["DefaultMaterial"]
    if not material_names:
        raise ValueError("material_names must contain at least one entry")

    if layer_map is None:
        layer_map = np.zeros((size, size), dtype=np.uint8)
    elif layer_map.shape != (size, size):
        raise ValueError(f"layer_map shape {layer_map.shape} must match {(size, size)}")
    layer_map = layer_map.astype(np.uint8, copy=False)

    min_m = float(heightmap_m.min())
    max_m = float(heightmap_m.max())
    rng = max(max_m - min_m, 1e-6)
    quant16 = ((heightmap_m - min_m) / rng * 65535.0).clip(0, 65535).astype("<u2")

    # Y-flip for the heightmap: .ter row 0 = south edge.
    # Our heightmap convention is row 0 = north (image-style), so flip vertically.
    flipped_h = quant16[::-1, :]
    # Layer map is built in terrain space (south-bottom) by Phase 4, so don't flip it
    # here. Phase 2 just supplies an all-zero map.
    flipped_layers = layer_map  # no flip per MapNG comment in exportTer.js

    out_path.parent.mkdir(parents=True, exist_ok=True)

    name_blocks = []
    for name in material_names:
        encoded = name.encode("utf-8")
        if len(encoded) < 255:
            name_blocks.append(bytes([len(encoded)]) + encoded)
        else:
            name_blocks.append(b"\xff" + len(encoded).to_bytes(2, "little") + encoded)

    # layerTextureMap: per-pixel texture variant index. We don't use
    # multi-variant textures yet, so it's all zeros (same byte count as
    # layer_map). This block must exist in the file or BeamNG's parser
    # gets out of sync reading materialCount.
    layer_texture_map = np.zeros((size, size), dtype=np.uint8)

    with open(out_path, "wb") as f:
        f.write(b"\x09")                                    # version
        f.write(size.to_bytes(4, "little"))
        f.write(flipped_h.tobytes())
        f.write(flipped_layers.tobytes())
        f.write(layer_texture_map.tobytes())
        f.write(len(material_names).to_bytes(4, "little"))
        for block in name_blocks:
            f.write(block)

    return min_m, max_m

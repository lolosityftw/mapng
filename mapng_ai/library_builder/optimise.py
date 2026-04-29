"""GLB optimiser — texture downscaling for performance presets.

Meshy textured GLBs are 50-80 MB each because they ship 2K PBR textures.
Per-frame draw cost in Three.js scales with texture *upload* size; downscaling
to 512² typically gives a 16× memory drop with imperceptible visual loss for
buildings seen from a distance.

We don't decimate geometry (yet) — Meshy preview meshes are already quite
low-poly. Texture size is the perf headline.

Quality presets (max texture dimension):
    ultra → original (no rewrite, just symlink/copy)
    high  → 1024
    medium → 512
    low   → 256
    minimum → 128
"""
from __future__ import annotations

import io
import json
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

from PIL import Image
from pygltflib import GLTF2

from mapng_ai import config


log = logging.getLogger(__name__)


# Maximum texture dimension per preset. None = keep originals untouched.
# (Geometry decimation with UV preservation is a TODO follow-up — it requires
# attribute-aware vertex collapse which fast-simplification supports but trimesh
# doesn't expose cleanly. Texture memory is the dominant browser bottleneck
# anyway.)
QUALITY_PRESETS: dict[str, int | None] = {
    "ultra":   None,
    "high":    1024,
    "medium":  512,
    "low":     256,
    "minimum": 128,
}


_OPT_CACHE = config.CACHE_DIR / "glb_optimised"


@dataclass(frozen=True)
class GlbStats:
    file_size: int
    image_count: int
    image_max_dim: int
    image_total_bytes: int
    triangle_count: int = 0


def stats(src_path: Path) -> GlbStats:
    """Cheap inspection: count textures, find the largest dimension, sum bytes."""
    if not src_path.exists():
        return GlbStats(0, 0, 0, 0)
    file_size = src_path.stat().st_size
    try:
        gltf = GLTF2().load(src_path)
    except Exception:
        return GlbStats(file_size, 0, 0, 0)

    images = gltf.images or []
    blob = gltf.binary_blob() or b""
    max_dim = 0
    total_bytes = 0
    for img in images:
        if img.bufferView is None:
            continue
        bv = gltf.bufferViews[img.bufferView]
        chunk = blob[bv.byteOffset : (bv.byteOffset or 0) + bv.byteLength]
        total_bytes += len(chunk)
        try:
            with Image.open(io.BytesIO(chunk)) as pil:
                w, h = pil.size
                max_dim = max(max_dim, w, h)
        except Exception:
            pass
    # Triangle count via trimesh load (cheap-ish, only does this for stats calls)
    tri_count = 0
    try:
        import trimesh
        scene = trimesh.load(src_path, force="scene")
        for g in scene.geometry.values():
            tri_count += len(g.faces)
    except Exception:
        pass

    return GlbStats(
        file_size=file_size,
        image_count=len(images),
        image_max_dim=max_dim,
        image_total_bytes=total_bytes,
        triangle_count=tri_count,
    )


# ---------------------------------------------------------------------------
def cache_path(src_path: Path, quality: str) -> Path:
    """Where the optimised version of `src_path` at `quality` lives on disk."""
    if quality == "ultra":
        return src_path
    rel = src_path.stem
    return _OPT_CACHE / quality / f"{rel}.glb"


def downsize_glb(src_path: Path, dst_path: Path, max_dim: int) -> None:
    """Re-encode every embedded image to ≤ max_dim on its longest side, JPEG
    when source was JPEG (preserves sRGB diffuse), PNG otherwise."""
    dst_path.parent.mkdir(parents=True, exist_ok=True)
    gltf = GLTF2().load(src_path)
    images = gltf.images or []
    if not images:
        shutil.copyfile(src_path, dst_path)
        return

    # Walk the blob, collect (bufferView, new_bytes) for replaced images
    blob = gltf.binary_blob() or b""
    new_image_bytes: dict[int, bytes] = {}   # bufferView_idx → new bytes

    for img in images:
        if img.bufferView is None:
            continue
        bv = gltf.bufferViews[img.bufferView]
        chunk = blob[bv.byteOffset : (bv.byteOffset or 0) + bv.byteLength]
        try:
            with Image.open(io.BytesIO(chunk)) as pil:
                if max(pil.size) <= max_dim:
                    continue
                pil = pil.copy()
                pil.thumbnail((max_dim, max_dim), Image.LANCZOS)
                buf = io.BytesIO()
                mime = (img.mimeType or "image/png").lower()
                if "jpeg" in mime or "jpg" in mime:
                    if pil.mode != "RGB":
                        pil = pil.convert("RGB")
                    pil.save(buf, format="JPEG", quality=86, optimize=True)
                else:
                    pil.save(buf, format="PNG", optimize=True)
                new_image_bytes[img.bufferView] = buf.getvalue()
        except Exception as exc:
            log.debug("skip image bv=%d: %s", img.bufferView, exc)

    if not new_image_bytes:
        # Already small enough
        shutil.copyfile(src_path, dst_path)
        return

    # Rebuild the binary blob: keep non-image data offsets stable but rewrite
    # the bufferView ranges for replaced images. Easiest correct approach is
    # to build a brand new blob.
    new_blob = bytearray()
    for bv_idx, bv in enumerate(gltf.bufferViews):
        if bv.byteOffset is None:
            bv.byteOffset = 0
        old_chunk = blob[bv.byteOffset : bv.byteOffset + bv.byteLength]
        chunk = new_image_bytes.get(bv_idx, old_chunk)
        # Pad to 4-byte alignment per glTF spec
        while len(new_blob) % 4 != 0:
            new_blob.append(0)
        bv.byteOffset = len(new_blob)
        bv.byteLength = len(chunk)
        new_blob.extend(chunk)

    # Single buffer covering the whole blob
    if gltf.buffers:
        gltf.buffers[0].byteLength = len(new_blob)

    gltf.set_binary_blob(bytes(new_blob))
    gltf.save_binary(dst_path)


def optimise(src_path: Path, quality: str) -> Path:
    """Return path to the GLB at the requested quality, generating + caching
    on first request."""
    if quality not in QUALITY_PRESETS:
        raise ValueError(f"unknown quality: {quality}")
    max_dim = QUALITY_PRESETS[quality]
    if max_dim is None:                     # ultra → original
        return src_path

    dst = cache_path(src_path, quality)
    if dst.exists() and dst.stat().st_size > 0 and dst.stat().st_mtime >= src_path.stat().st_mtime:
        return dst

    try:
        downsize_glb(src_path, dst, max_dim)
    except Exception as exc:
        log.warning("optimise %s @ %s failed: %s -- falling back to source",
                    src_path.name, quality, exc)
        return src_path
    return dst


def estimate_gpu_texture_bytes(s: GlbStats) -> int:
    """Rough GPU memory cost = max_dim² × 4 bytes × image_count (uncompressed)."""
    return s.image_max_dim * s.image_max_dim * 4 * s.image_count


def stats_for_all_qualities(src_path: Path) -> dict[str, dict]:
    """Lazy stats per preset — only compares file sizes, no GLB rewrite."""
    out = {}
    base = stats(src_path)
    out["ultra"] = {
        "file_size": base.file_size, "image_max_dim": base.image_max_dim,
        "image_count": base.image_count, "image_total_bytes": base.image_total_bytes,
        "triangle_count": base.triangle_count,
        "gpu_texture_bytes": estimate_gpu_texture_bytes(base),
        "exists": src_path.exists(),
    }
    for q in ("high", "medium", "low", "minimum"):
        cp = cache_path(src_path, q)
        if cp.exists() and cp.stat().st_size > 0:
            s = stats(cp)
            out[q] = {
                "file_size": s.file_size, "image_max_dim": s.image_max_dim,
                "image_count": s.image_count, "image_total_bytes": s.image_total_bytes,
                "triangle_count": s.triangle_count,
                "gpu_texture_bytes": estimate_gpu_texture_bytes(s),
                "exists": True,
            }
        else:
            target_dim = QUALITY_PRESETS[q] or 0
            out[q] = {
                "exists": False, "max_dim_target": target_dim,
                # Predicted GPU bytes if we built at this quality
                "gpu_texture_bytes": (target_dim * target_dim * 4 * base.image_count) if target_dim else 0,
            }
    return out

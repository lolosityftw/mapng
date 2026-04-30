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


@dataclass(frozen=True)
class QualityPreset:
    max_texture_dim: int | None    # None = keep original
    target_triangles: int | None   # None = keep original; cap per-mesh count


# Triangle-target keys map to texture-resolution + decimation pairs.
# `original` is the only escape hatch (no processing); the five user-facing
# variants are the triangle-count keys the user selects from.
QUALITY_PRESETS: dict[str, QualityPreset] = {
    "1.5k":     QualityPreset(max_texture_dim=128,   target_triangles=1_500),
    "5k":       QualityPreset(max_texture_dim=256,   target_triangles=5_000),
    "10k":      QualityPreset(max_texture_dim=512,   target_triangles=10_000),
    "50k":      QualityPreset(max_texture_dim=1024,  target_triangles=50_000),
    "100k":     QualityPreset(max_texture_dim=2048,  target_triangles=100_000),
    "original": QualityPreset(max_texture_dim=None,  target_triangles=None),
}

DEFAULT_QUALITY = "10k"


# Active quality setting persists in a single line file. The library page is
# the only thing that writes to it; the main pipeline just reads it.
_ACTIVE_QUALITY_FILE = config.CACHE_DIR / "active_quality.txt"


def get_active_quality() -> str:
    if _ACTIVE_QUALITY_FILE.exists():
        try:
            v = _ACTIVE_QUALITY_FILE.read_text(encoding="utf-8").strip()
            if v in QUALITY_PRESETS:
                return v
        except Exception:
            pass
    return DEFAULT_QUALITY


def set_active_quality(quality: str) -> str:
    if quality not in QUALITY_PRESETS:
        raise ValueError(f"unknown quality: {quality}")
    _ACTIVE_QUALITY_FILE.parent.mkdir(parents=True, exist_ok=True)
    _ACTIVE_QUALITY_FILE.write_text(quality + "\n", encoding="utf-8")
    return quality


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
    if quality == "original":
        return src_path
    rel = src_path.stem
    return _OPT_CACHE / quality / f"{rel}.glb"


def decimate_glb(src_path: Path, dst_path: Path, target_triangles: int) -> Path:
    """Reduce per-mesh triangle count via fast-simplification while preserving
    UVs through collapse replay. Materials are kept (single texture set per
    mesh). Returns the destination path on success, src_path on failure.
    """
    import numpy as np
    import trimesh
    import fast_simplification as fs

    scene = trimesh.load(src_path, force="scene", process=False)
    if not isinstance(scene, trimesh.Scene):
        scene = trimesh.Scene(scene)

    new_scene = trimesh.Scene()
    for name, geom in scene.geometry.items():
        if not isinstance(geom, trimesh.Trimesh):
            new_scene.add_geometry(geom, geom_name=name)
            continue

        face_count = len(geom.faces)
        if face_count <= target_triangles:
            new_scene.add_geometry(geom, geom_name=name)
            continue

        # Meshy-refine GLBs ship as 'exploded' meshes — every face has its own
        # 3 vertices, none shared. fast-simplification can't break topological
        # walls without welding first. merge_vertices with merge_tex+merge_norm
        # collapses positionally-coincident verts so the simplifier can do its
        # job; UV seams near texture boundaries are sacrificed (acceptable at
        # game-distance viewing).
        welded = geom.copy()
        try:
            welded.merge_vertices(merge_tex=True, merge_norm=True, digits_vertex=4)
        except Exception:
            pass

        verts32 = np.ascontiguousarray(welded.vertices, dtype=np.float32)
        faces32 = np.ascontiguousarray(welded.faces, dtype=np.int32)
        verts32 = np.nan_to_num(verts32, nan=0.0, posinf=0.0, neginf=0.0)

        try:
            new_pts, new_faces, collapses = fs.simplify(
                verts32, faces32, target_count=int(target_triangles), return_collapses=True,
            )
        except Exception as exc:
            log.warning("decimate %s: simplify failed (%s) -- keeping original", name, exc)
            new_scene.add_geometry(geom, geom_name=name)
            continue

        # Strip any NaN/inf the simplifier introduced — JSON serialisation
        # in pygltflib chokes on them.
        new_pts = np.nan_to_num(new_pts, nan=0.0, posinf=0.0, neginf=0.0)

        new_visual = welded.visual
        # Replay UVs if the welded mesh has them
        try:
            uv = getattr(welded.visual, "uv", None)
            if uv is not None and len(uv) == len(verts32):
                # Pad to 3D and sanitise inputs (Meshy occasionally emits NaN UVs)
                uv_clean = np.nan_to_num(np.asarray(uv, dtype=np.float32),
                                         nan=0.0, posinf=0.0, neginf=0.0)
                uv_padded = np.column_stack([
                    uv_clean[:, 0],
                    uv_clean[:, 1],
                    np.zeros(len(uv_clean), dtype=np.float32),
                ])
                new_uv_padded, _, _ = fs.replay_simplification(uv_padded, faces32, collapses)
                new_uv = np.nan_to_num(new_uv_padded[:, :2], nan=0.0, posinf=0.0, neginf=0.0)
                new_visual = trimesh.visual.TextureVisuals(
                    uv=new_uv, material=welded.visual.material,
                )
        except Exception as exc:
            log.warning("decimate %s: UV replay failed (%s) -- using collapsed mesh without UVs", name, exc)

        new_geom = trimesh.Trimesh(
            vertices=new_pts, faces=new_faces, visual=new_visual, process=False,
        )
        new_scene.add_geometry(new_geom, geom_name=name)

    dst_path.parent.mkdir(parents=True, exist_ok=True)
    new_scene.export(dst_path)
    return dst_path


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
    on first request. Two-pass: decimate geometry → downscale textures."""
    if quality not in QUALITY_PRESETS:
        raise ValueError(f"unknown quality: {quality}")
    preset = QUALITY_PRESETS[quality]
    if preset.max_texture_dim is None and preset.target_triangles is None:
        return src_path     # original: no processing

    dst = cache_path(src_path, quality)
    if dst.exists() and dst.stat().st_size > 0 and dst.stat().st_mtime >= src_path.stat().st_mtime:
        return dst

    # Two-pass: decimate → texture downscale. We pipe through a temp file so the
    # decimation result is the input to the texture pass.
    temp = dst.with_suffix(".tmp.glb")
    try:
        if preset.target_triangles is not None:
            decimate_glb(src_path, temp, preset.target_triangles)
            decimated_src = temp
        else:
            decimated_src = src_path

        if preset.max_texture_dim is not None:
            downsize_glb(decimated_src, dst, preset.max_texture_dim)
        else:
            shutil.copyfile(decimated_src, dst)
    except Exception as exc:
        log.warning("optimise %s @ %s failed: %s -- falling back to source",
                    src_path.name, quality, exc)
        if temp.exists():
            try: temp.unlink()
            except Exception: pass
        return src_path
    finally:
        if temp.exists():
            try: temp.unlink()
            except Exception: pass
    return dst


def estimate_gpu_texture_bytes(s: GlbStats) -> int:
    """Rough GPU memory cost = max_dim² × 4 bytes × image_count (uncompressed)."""
    return s.image_max_dim * s.image_max_dim * 4 * s.image_count


def stats_for_all_qualities(src_path: Path) -> dict[str, dict]:
    """Lazy stats per preset — only compares file sizes, no GLB rewrite."""
    out = {}
    base = stats(src_path)
    out["original"] = {
        "file_size": base.file_size, "image_max_dim": base.image_max_dim,
        "image_count": base.image_count, "image_total_bytes": base.image_total_bytes,
        "triangle_count": base.triangle_count,
        "gpu_texture_bytes": estimate_gpu_texture_bytes(base),
        "exists": src_path.exists(),
    }
    for q in ("100k", "50k", "10k", "5k", "1.5k"):
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
            preset = QUALITY_PRESETS[q]
            target_dim = preset.max_texture_dim or 0
            out[q] = {
                "exists": False,
                "max_dim_target": target_dim,
                "target_triangles": preset.target_triangles,
                # Predicted GPU bytes if we built at this quality
                "gpu_texture_bytes": (target_dim * target_dim * 4 * base.image_count) if target_dim else 0,
            }
    return out

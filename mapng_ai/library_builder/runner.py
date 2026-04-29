"""Batch builder — generates the entire region asset library via Meshy.

Designed to be called once per region pack and idempotent: re-running skips
entries whose GLB is already on disk. Streams progress events that the API
endpoint can forward over SSE.
"""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

from mapng_ai import config
from mapng_ai.assets.meshy import MeshyEngine
from mapng_ai.library_builder.catalogue import CATALOGUE, CatalogueEntry, by_category


log = logging.getLogger(__name__)


_BUILDINGS_DIR = config.ROOT / "assets" / "buildings"
_TREES_DIR = config.ROOT / "assets" / "trees"
_VEHICLES_DIR = config.ROOT / "assets" / "vehicles"


def target_dir(entry: CatalogueEntry) -> Path:
    base = {"building": _BUILDINGS_DIR, "tree": _TREES_DIR, "vehicle": _VEHICLES_DIR}[entry.category]
    return base / entry.type


def target_glb(entry: CatalogueEntry) -> Path:
    return target_dir(entry) / f"{entry.slug}.glb"


def manifest_path(entry: CatalogueEntry) -> Path:
    return target_dir(entry) / "manifest.json"


@dataclass
class LibraryProgress:
    total: int
    completed: int
    skipped: int
    failed: int
    in_progress: list[str]


Emit = Callable[[str, dict], Awaitable[None]]


async def _gen_one(entry: CatalogueEntry, engine: MeshyEngine, sem: asyncio.Semaphore,
                   progress: LibraryProgress, emit: Emit | None) -> bool:
    """Generate a single entry. Returns True on success/skip, False on failure."""
    glb = target_glb(entry)
    if glb.exists() and glb.stat().st_size > 0:
        progress.skipped += 1
        progress.completed += 1
        if emit:
            await emit("entry:skip", {"slug": entry.slug, "reason": "cached"})
        return True

    target_dir(entry).mkdir(parents=True, exist_ok=True)
    progress.in_progress.append(entry.slug)
    if emit:
        await emit("entry:start", {"slug": entry.slug, "category": entry.category,
                                   "type": entry.type, "prompt": entry.prompt})

    async with sem:
        path = await engine.generate(entry.prompt, seed=hash(entry.slug) & 0xFFFFFFFF)

    if path is None:
        progress.failed += 1
        progress.completed += 1
        if entry.slug in progress.in_progress:
            progress.in_progress.remove(entry.slug)
        if emit:
            await emit("entry:fail", {"slug": entry.slug})
        return False

    # Copy from Meshy cache into the region asset folder
    shutil.copyfile(path, glb)
    # Append/update manifest.json for this type
    manifest = {}
    if manifest_path(entry).exists():
        try:
            manifest = json.loads(manifest_path(entry).read_text(encoding="utf-8"))
        except Exception:
            manifest = {}
    manifest[glb.name] = {
        "slug": entry.slug,
        "footprint_m": list(entry.footprint_m),
        "levels": entry.levels,
        "prompt": entry.prompt,
    }
    manifest_path(entry).write_text(
        json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
    )

    progress.completed += 1
    if entry.slug in progress.in_progress:
        progress.in_progress.remove(entry.slug)
    if emit:
        await emit("entry:done", {"slug": entry.slug,
                                  "size_bytes": glb.stat().st_size,
                                  "completed": progress.completed,
                                  "total": progress.total})
    return True


async def build_library(
    *,
    categories: list[str] | None = None,
    concurrency: int | None = None,
    emit: Emit | None = None,
) -> LibraryProgress:
    engine = MeshyEngine(max_concurrency=concurrency)
    if not engine.configured:
        raise RuntimeError(
            "Meshy is not configured. Set MAPNG_API_ENGINE=meshy and MAPNG_API_KEY in .env."
        )

    if categories:
        entries = [e for e in CATALOGUE if e.category in categories]
    else:
        entries = list(CATALOGUE)

    progress = LibraryProgress(
        total=len(entries), completed=0, skipped=0, failed=0, in_progress=[]
    )
    if emit:
        await emit("batch:start", {
            "total": progress.total,
            "categories": sorted({e.category for e in entries}),
            "concurrency": engine.concurrency,
            "rps": engine.rps,
            "texture": engine.texture,
        })

    # The engine already enforces its own semaphore + rate limiter, so we
    # don't double-cap here.
    sem = asyncio.Semaphore(engine.concurrency)
    await asyncio.gather(*[_gen_one(e, engine, sem, progress, emit) for e in entries])

    if emit:
        await emit("batch:done", {"total": progress.total,
                                  "skipped": progress.skipped,
                                  "failed": progress.failed,
                                  "completed": progress.completed})
    return progress


def library_status() -> dict:
    """Quick snapshot of what's already on disk for the UI."""
    by_cat: dict[str, dict[str, int]] = {"building": {}, "tree": {}, "vehicle": {}}
    for e in CATALOGUE:
        glb = target_glb(e)
        by_cat[e.category].setdefault(e.type, 0)
        if glb.exists() and glb.stat().st_size > 0:
            by_cat[e.category][e.type] += 1
    totals = {cat: sum(types.values()) for cat, types in by_cat.items()}
    return {
        "totals": totals,
        "by_category": by_cat,
        "catalogue_size": len(CATALOGUE),
    }

"""Phase 0 pipeline scaffolding.

Each stage is a stub that emits progress events. Real implementations land
phase-by-phase per docs/SPEC.md §8.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import AsyncIterator, Awaitable, Callable


@dataclass(frozen=True)
class BBox:
    west: float
    south: float
    east: float
    north: float

    @property
    def width_deg(self) -> float:
        return self.east - self.west

    @property
    def height_deg(self) -> float:
        return self.north - self.south


@dataclass(frozen=True)
class Stage:
    key: str
    label: str
    seconds: float  # stub duration so the UI shows real-feeling progress

    async def run(self, bbox: BBox, emit: "Emit") -> None:
        """Phase 0: just sleep. Real implementations replace this."""
        await emit("stage:start", {"key": self.key, "label": self.label})
        steps = 4
        for i in range(steps):
            await asyncio.sleep(self.seconds / steps)
            await emit(
                "stage:progress",
                {"key": self.key, "fraction": (i + 1) / steps},
            )
        await emit("stage:done", {"key": self.key})


STAGES: tuple[Stage, ...] = (
    Stage("region", "Resolve region", 0.4),
    Stage("fetch", "Fetch DEM / imagery / OSM", 1.6),
    Stage("heightmap", "Build heightmap", 0.8),
    Stage("segment", "Land-cover segmentation", 1.2),
    Stage("splat", "Material splatting", 0.6),
    Stage("place", "Object placement", 1.4),
    Stage("export", "BeamNG export", 1.6),
)


Emit = Callable[[str, dict], Awaitable[None]]


async def run_pipeline(bbox: BBox, emit: Emit) -> None:
    await emit("pipeline:start", {"bbox": bbox.__dict__, "stages": [s.key for s in STAGES]})
    for stage in STAGES:
        await stage.run(bbox, emit)
    await emit("pipeline:done", {})

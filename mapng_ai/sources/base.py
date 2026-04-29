"""Source protocols — Phase 1 onwards."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import numpy as np


@dataclass(frozen=True)
class BBoxLL:
    """Bounding box in EPSG:4326 (lat/lon, degrees)."""
    west: float
    south: float
    east: float
    north: float


@dataclass(frozen=True)
class BBoxITM:
    """Bounding box in EPSG:2157 (Irish Transverse Mercator, metres)."""
    west: float
    south: float
    east: float
    north: float

    @property
    def width_m(self) -> float:
        return self.east - self.west

    @property
    def height_m(self) -> float:
        return self.north - self.south


@dataclass(frozen=True)
class ElevationTile:
    """A patch of elevation data with its georeferencing."""
    elevations_m: np.ndarray          # float32 array (rows, cols), units = metres above sea level
    bbox_4326: BBoxLL                 # extent in lat/lon
    no_data_value: float = -32768.0


class ElevationSource(Protocol):
    name: str
    """Human-readable identifier shown in the UI ('OSNI 1m', 'Terrarium 30m', …)."""

    native_resolution_m: float
    """Approximate ground resolution this source delivers."""

    async def covers(self, bbox: BBoxLL) -> bool:
        """Cheap availability check before fetching."""
        ...

    async def fetch(self, bbox: BBoxLL) -> ElevationTile:
        """Return elevations covering AT LEAST `bbox` (may be slightly larger)."""
        ...

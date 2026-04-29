"""Source router — decides which elevation source covers a bbox.

Phase 1: Terrarium only. Real OSNI integration is a Phase 7 follow-up
(OpenDataNI scraping is non-trivial and out of scope for the MVP).
"""
from __future__ import annotations

from mapng_ai.sources.base import BBoxLL, ElevationSource
from mapng_ai.sources.terrarium import default_source as _default_terrarium


async def select_elevation_source(bbox: BBoxLL) -> ElevationSource:
    return _default_terrarium()

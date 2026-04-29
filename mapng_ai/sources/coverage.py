"""Source router — picks the best elevation source covering the bbox.

Order: OSNI 1 m local tiles (best) → Terrarium 9 m global (fallback). Add
more entries here as new adapters land.
"""
from __future__ import annotations

from mapng_ai.sources.base import BBoxLL, ElevationSource
from mapng_ai.sources.osni import OSNISource
from mapng_ai.sources.terrarium import default_source as _default_terrarium


async def select_elevation_source(bbox: BBoxLL) -> ElevationSource:
    osni = OSNISource()
    if await osni.covers(bbox):
        return osni
    return _default_terrarium()

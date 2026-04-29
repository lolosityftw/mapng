"""Overpass API client.

Single shared query that pulls everything later phases need (buildings, roads,
landuse, water) so we hit the API once per generation. Results are cached on
disk by query hash so re-runs are instant.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from mapng_ai import config
from mapng_ai.sources.base import BBoxLL


OVERPASS_URL = "https://overpass-api.de/api/interpreter"
USER_AGENT = "mapng-ai/0.1 (https://github.com/anthropics/claude-code)"


def _query(bbox: BBoxLL) -> str:
    s, w, n, e = bbox.south, bbox.west, bbox.north, bbox.east
    bb = f"{s},{w},{n},{e}"
    return f"""
[out:json][timeout:60];
(
  way["building"]({bb});
  way["highway"]({bb});
  way["landuse"]({bb});
  way["natural"]({bb});
  way["waterway"]({bb});
  relation["building"]({bb});
  relation["landuse"]({bb});
  relation["natural"]({bb});
);
out body;
>;
out skel qt;
""".strip()


@dataclass(frozen=True)
class OSMData:
    """Parsed Overpass response. Geometries reconstructed by ID lookup."""
    nodes: dict[int, tuple[float, float]]   # id → (lat, lon)
    ways: list[dict]                        # raw Overpass `way` elements
    relations: list[dict]
    raw_path: Path


async def fetch_osm(bbox: BBoxLL) -> OSMData:
    cache_dir = config.CACHE_DIR / "overpass"
    cache_dir.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha1(_query(bbox).encode()).hexdigest()[:16]
    cache_file = cache_dir / f"{key}.json"

    if cache_file.exists():
        raw = json.loads(cache_file.read_text(encoding="utf-8"))
    else:
        async with httpx.AsyncClient(
            timeout=120.0,
            headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"},
        ) as client:
            attempts = 3
            for i in range(attempts):
                r = await client.post(OVERPASS_URL, data={"data": _query(bbox)})
                if r.status_code == 200:
                    break
                if r.status_code in (429, 504, 502, 503) and i < attempts - 1:
                    await asyncio.sleep(2 ** i + 1)
                    continue
                r.raise_for_status()
            raw = r.json()
        cache_file.write_text(json.dumps(raw), encoding="utf-8")

    nodes: dict[int, tuple[float, float]] = {}
    ways: list[dict] = []
    relations: list[dict] = []
    for el in raw.get("elements", []):
        t = el.get("type")
        if t == "node":
            nodes[el["id"]] = (el["lat"], el["lon"])
        elif t == "way":
            ways.append(el)
        elif t == "relation":
            relations.append(el)
    return OSMData(nodes=nodes, ways=ways, relations=relations, raw_path=cache_file)


def way_polygon_ll(way: dict, nodes: dict[int, tuple[float, float]]) -> list[tuple[float, float]] | None:
    """Return (lon, lat) ring if way is a closed polygon, else None."""
    pts: list[tuple[float, float]] = []
    for nid in way.get("nodes", []):
        ll = nodes.get(nid)
        if ll is None:
            return None
        lat, lon = ll
        pts.append((lon, lat))
    if len(pts) < 3 or pts[0] != pts[-1]:
        return None
    return pts


def way_line_ll(way: dict, nodes: dict[int, tuple[float, float]]) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for nid in way.get("nodes", []):
        ll = nodes.get(nid)
        if ll is None:
            continue
        out.append((ll[1], ll[0]))   # (lon, lat)
    return out

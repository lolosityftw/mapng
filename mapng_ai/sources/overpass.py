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


OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",       # main (sometimes 504s)
    "https://overpass.kumi.systems/api/interpreter", # community mirror
    "https://overpass.openstreetmap.ru/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]
USER_AGENT = "mapng-ai/0.1 (https://github.com/anthropics/claude-code)"


def _query(bbox: BBoxLL) -> str:
    s, w, n, e = bbox.south, bbox.west, bbox.north, bbox.east
    bb = f"{s},{w},{n},{e}"
    # Beefed-up query: explicitly ask for relation members + barrier/power
    # networks + leisure (parks/pitches) so the pipeline gets in-field
    # boundaries that simple way queries miss. Multipolygon farmland
    # relations carry inner field outlines that ways alone don't expose.
    return f"""
[out:json][timeout:90];
(
  way["building"]({bb});
  way["highway"]({bb});
  way["landuse"]({bb});
  way["natural"]({bb});
  way["leisure"]({bb});
  way["waterway"]({bb});
  way["barrier"]({bb});
  way["power"]({bb});
  way["junction"]({bb});

  relation["building"]({bb});
  relation["landuse"]({bb});
  relation["natural"]({bb});
  relation["leisure"]({bb});
  relation["waterway"]({bb});
  relation["boundary"="land_area"]({bb});
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


async def _fetch_overpass_with_fallback(query: str) -> dict:
    """Try each endpoint with retries. Raises a clear RuntimeError on full
    failure so the pipeline can surface a useful message."""
    last_status = None
    last_text = ""
    headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"}
    timeout = httpx.Timeout(connect=10.0, read=180.0, write=30.0, pool=10.0)
    attempts_per_endpoint = 3

    async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
        for endpoint in OVERPASS_ENDPOINTS:
            for i in range(attempts_per_endpoint):
                try:
                    r = await client.post(endpoint, data={"data": query})
                except (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError) as exc:
                    last_status = "network"
                    last_text = f"{type(exc).__name__}: {exc}"
                    if i < attempts_per_endpoint - 1:
                        await asyncio.sleep(2 ** i + 1)
                        continue
                    break       # try next endpoint
                if r.status_code == 200:
                    return r.json()
                last_status = r.status_code
                last_text = r.text[:200]
                # 429/5xx → retry on this endpoint, then move on; 4xx else → bail entirely
                if r.status_code in (429, 502, 503, 504, 524):
                    if i < attempts_per_endpoint - 1:
                        await asyncio.sleep(2 ** i + 2)
                        continue
                    break       # next endpoint
                r.raise_for_status()
    raise RuntimeError(
        f"All Overpass endpoints failed. Last status: {last_status}. {last_text}"
    )


async def fetch_osm(bbox: BBoxLL) -> OSMData:
    cache_dir = config.CACHE_DIR / "overpass"
    cache_dir.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha1(_query(bbox).encode()).hexdigest()[:16]
    cache_file = cache_dir / f"{key}.json"

    if cache_file.exists():
        raw = json.loads(cache_file.read_text(encoding="utf-8"))
    else:
        raw = await _fetch_overpass_with_fallback(_query(bbox))
        cache_file.write_text(json.dumps(raw), encoding="utf-8")

    nodes: dict[int, tuple[float, float]] = {}
    ways: list[dict] = []
    ways_by_id: dict[int, dict] = {}
    relations: list[dict] = []
    for el in raw.get("elements", []):
        t = el.get("type")
        if t == "node":
            nodes[el["id"]] = (el["lat"], el["lon"])
        elif t == "way":
            ways.append(el)
            ways_by_id[el["id"]] = el
        elif t == "relation":
            relations.append(el)

    # Inherit relation tags down to member ways. A `multipolygon` relation
    # tagged `landuse=farmland` has lots of unmapped inner-ring ways that
    # carry the field's polygon geometry but no tags of their own. The
    # field-boundary synthesiser only looks at way-level tags, so without
    # this flattening it misses every relation-based field — exactly the
    # case in rural Cookstown where most farmland is mapped as relations.
    _PROPAGATE_TAGS = ("landuse", "natural", "leisure", "waterway", "boundary")
    for rel in relations:
        rtags = rel.get("tags") or {}
        if rtags.get("type") not in ("multipolygon", "boundary", None):
            continue
        propagated = {k: v for k, v in rtags.items() if k in _PROPAGATE_TAGS}
        if not propagated:
            continue
        for member in rel.get("members") or []:
            if member.get("type") != "way":
                continue
            wid = member.get("ref")
            w = ways_by_id.get(wid)
            if w is None:
                continue
            wtags = w.setdefault("tags", {})
            # Only inject if the way doesn't already declare its own
            # version of that key — never overwrite explicit ways.
            for k, v in propagated.items():
                wtags.setdefault(k, v)

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

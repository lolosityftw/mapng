# MapNG-AI

Locally-run tool that generates [BeamNG.drive](https://www.beamng.com/) maps from real-world locations.

> **Status:** v0.1 — full pipeline working end-to-end. See [docs/SPEC.md](docs/SPEC.md).

## Quick start

```bash
python -m pip install --user uv      # one-time
python -m uv sync                    # install deps
python -m uv run python app.py       # run server
```

Open http://127.0.0.1:8000/ — draw a bounding box on the map, click Generate, watch progress stream into the right pane.

The generated `.zip` from the Artifacts list goes into your BeamNG `mods/` folder:

```
%localappdata%\BeamNG.drive\<version>\mods\
```

## Pipeline stages

1. Region resolve — bbox → Irish Transverse Mercator
2. Fetch DEM (Terrarium / OSNI) + OSM (Overpass) + imagery (Esri) in parallel
3. Heightmap — stitch + fill + bilaplacian relax + reproject to ITM
4. Segment — OSM rasterise + HSV imagery classifier (8 land cover classes)
5. Splat — per-class opacity layers + procedural PBR-ish diffuse + combined preview
6. Place — buildings (per-type DAEs), foliage (Poisson-disk trees + OSM hedges), decal roads
7. Export — full BeamNG level package as a ZIP

## Optional: Meshy AI text-to-3D buildings

Meshy generates real 3D meshes from text prompts seeded by OSM building tags
("two-storey traditional semi-detached house, slate roof, exterior").
Replaces the largest N placeholder buildings per generation.

### Setup

Create a `.env` file at the project root (gitignored):

```
MAPNG_API_ENGINE=meshy
MAPNG_API_KEY=<your meshy key>
MAPNG_MESHY_MAX_BUILDINGS=12
MAPNG_MESHY_MIN_AREA_M2=120
```

Get an API key at https://www.meshy.ai (free tier has limited credits).

### How it works

- After normal placement, the N largest buildings (by footprint, ≥ MIN_AREA_M2)
  are queued to Meshy in parallel (concurrency 4)
- Each request takes ~30–90 s; cached on disk by prompt hash
- Subsequent runs hitting the same prompt+seed are instant (free)
- Other buildings stay placeholders
- Disable any time by removing `MAPNG_API_ENGINE` from `.env`

### Cost control

Meshy charges per generation. With `MAPNG_MESHY_MAX_BUILDINGS=12` a cold first
run uses 12 credits (~$1–2 at preview tier). Re-runs of the same map use 0
because of the cache.

## Optional: OSNI 1 m LiDAR

Drop OpenDataNI DTM GeoTIFFs into `assets/osni/dtm/`. The coverage router
auto-detects them and uses 1 m terrain wherever covered, falling back to
Terrarium 9 m elsewhere.

## Optional: Library buildings

Drop CC0 / CC-BY building meshes into `assets/buildings/<type>/`:

```
assets/buildings/
├── residential/
│   ├── manifest.json   (optional — overrides footprint/levels)
│   └── house_a.dae
├── commercial/
│   └── shop_b.glb
└── industrial/
    └── warehouse_c.dae
```

`LibraryProvider` auto-discovers these. Provider chain: API → Library → Placeholder.

## Project layout

```
mapng_ai/
├── app.py                 # FastAPI entry
├── config.py              # Paths, defaults
├── sources/               # Data fetchers
│   ├── terrarium.py       # AWS global DEM
│   ├── osni.py            # NI 1 m LiDAR (manual tiles)
│   ├── esri.py            # World Imagery
│   ├── overpass.py        # OSM
│   └── coverage.py        # Source router
├── pipeline/              # Stage 1, 3-7
│   ├── region.py          # CRS / bbox math
│   ├── heightmap.py       # Stitch + fill + relax + reproject
│   ├── imagery.py         # Stitch + reproject + normal map
│   ├── classmap.py        # OSM rasterise → land classes
│   ├── hsv_seg.py         # HSV imagery classifier
│   ├── splatting.py       # Per-class opacity layers
│   ├── textures.py        # Procedural PBR-ish ground textures
│   ├── placement.py       # OSM buildings → world placements
│   ├── foliage.py         # Trees (Poisson-disk) + hedges
│   ├── decal_roads.py     # OSM highways → DecalRoad objects
│   ├── beamng_ter.py      # .ter binary writer
│   └── beamng_level.py    # Level zip writer
├── ai/                    # (reserved for AI seg model)
├── assets/                # Asset providers
│   ├── placeholder.py     # Per-type pitched/flat-roof DAEs
│   ├── library.py         # User folder of GLB/DAE
│   ├── meshy.py           # Meshy text-to-3D engine
│   ├── api_provider.py    # Generic API scaffold
│   └── chain.py           # API → Library → Placeholder fallback chain
└── ui/
    ├── templates/index.html
    └── static/{app,preview}.{js,css}
```

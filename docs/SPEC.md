# MapNG-AI — Project Overview & Build Plan

> **Status:** v0.1 spec for implementation
> **Target:** Locally-run tool that generates [BeamNG.drive](http://BeamNG.drive) maps from real-world locations
> **Reference:** github.com/nikkiluzader/mapng (predecessor with known quality ceilings)

---

## 1. What you're building

MapNG-AI is a locally-run tool that turns any real-world location into a playable [BeamNG.drive](http://BeamNG.drive) map. The user picks an area on a map, the tool fetches geographic data automatically — terrain, aerial imagery, OpenStreetMap features — processes it through a smart pipeline, and writes out a BeamNG level the user can drive on. The whole thing runs on the user's machine and aims to produce a 2×2 km area in under 10 minutes.

The reference tool is MapNG (github.com/nikkiluzader/mapng). MapNG works but has three quality ceilings:

1. Buildings are featureless extruded boxes
2. Terrain detail is bounded by 30 m global SRTM data
3. Ground textures are flat satellite colormaps that look painted-on at car-windshield distance

MapNG-AI fixes all three by using better data sources, smart asset placement, and AI-driven semantic segmentation for ground materials.

### Core design principles

- **Local-first.** Single Python app, runs on the user's machine. APIs are used for data fetching but no cloud GPU workers.
- **Automatic data collection.** User gives it a bounding box; it figures out what's available (Northern Ireland LiDAR, global fallback DEMs, OSM, etc.) and pulls everything itself.
- **Pluggable assets.** Buildings and trees use simple coloured placeholders at first. Real GLB models or AI-generated meshes can be dropped in later — the placement system doesn't care which.
- **Reproducible output.** Same bounding box always produces the same map. Re-running won't shuffle the world underneath the user.
- **Honest fallbacks.** If high-resolution data isn't available for an area, the tool falls back to lower-resolution sources rather than failing.

---

## 2. How it works — pipeline overview

The pipeline has seven stages. Each stage produces an artifact the next stage consumes, so individual stages can be swapped or skipped during development.

| Stage | Input | Output | Time budget |
|---|---|---|---|
| 1. Region resolve | Bounding box (lat/lon) | Working CRS, tile list, data source plan | <5 s |
| 2. Data fetch | Tile list | DEM tiles, ortho tiles, OSM GeoJSON | 60–120 s |
| 3. Heightmap build | DEM tiles | Stitched, gap-filled 2048² heightmap | 20–30 s |
| 4. Land-cover segmentation | Ortho imagery | Per-pixel class map (grass/road/water/etc.) | 15–60 s |
| 5. Material splatting | Class map + OSM roads | BeamNG opacity layers per material | 15–30 s |
| 6. Object placement | OSM buildings + class map | Building, foliage, water, road decal placements | 60–120 s |
| 7. BeamNG export | All of the above | `.ter` file + level package ZIP | 60–120 s |

**Total target:** 4–8 minutes on a machine with a GPU, comfortably inside the 10-minute target. CPU-only adds 2–3 minutes mostly to stage 4.

---

## 3. Data sources & automatic collection

Everything in this section the tool collects automatically. The user never downloads a file by hand. The tool checks each source's availability for the requested area and picks the best option.

### 3.1 Elevation (DEM)

Priority order, best to worst:

| Source | Resolution | Coverage | Format |
|---|---|---|---|
| OSNI LiDAR DTM (river basin) | 1 m (some 0.2 m point cloud) | Most populated NI | GeoTIFF, Irish Grid |
| OSNI 10 m DTM | 10 m | All of NI | GeoTIFF |
| UK EA LiDAR (DEFRA) | 1 m / 2 m | Most of England, parts of UK | GeoTIFF |
| AWS Terrarium / SRTM | ~30 m global | Worldwide | PNG-encoded tiles |
| GPXZ (optional, paid) | Variable, often <10 m | Worldwide premium | API |

For a Northern Ireland location, the tool first queries OSNI's coverage index, takes 1 m LiDAR where available, falls back to 10 m DTM elsewhere, and uses Terrarium only as a last resort. Importantly: because LiDAR is already 1 m native data for most of populated NI, **terrain super-resolution AI is not needed for v1**. This deletes weeks of model-training work.

#### OSNI specifics

- **CRS:** Irish Grid (EPSG:29900) or Irish Transverse Mercator (EPSG:2157). The tool reprojects automatically with rasterio.
- **Coverage index:** OSNI publishes a tile index — the tool downloads it once at startup and caches it, then any tile fetch is a fast local lookup.
- **Licence:** Open Government Licence — free use including commercial, with attribution.
- **DTM vs DSM:** Always use DTM (bare earth, vegetation/buildings filtered out). DSM includes trees/buildings and is wrong for terrain.
- **Endpoints:** OpenDataNI ([opendatani.gov.uk](http://opendatani.gov.uk)) hosts the open datasets; some are direct downloads, some need scraping the dataset page for download links. Build a small adapter per dataset.

### 3.2 Aerial imagery (for segmentation)

| Source | Resolution | Coverage | Notes |
|---|---|---|---|
| OSNI orthophotography | 10 cm (commercial), open variant lower | All of NI | Verify per-dataset which is downloadable |
| Esri World Imagery | 30–60 cm urban, lower rural | Worldwide | Free for non-commercial via tile service |
| Bing Maps imagery | Similar to Esri | Worldwide | Requires API key |
| Sentinel-2 | 10 m | Worldwide, free, frequent | Lower-res fallback |

Note: this imagery is used as input to the segmentation model — it never gets stretched directly across terrain as the rendered texture. Lower resolution is fine here as long as land-cover classes can be distinguished. 30–60 cm is plenty.

### 3.3 OpenStreetMap

Single source: Overpass API ([overpass-api.de](http://overpass-api.de) or a self-hosted instance for heavy use). Queried automatically per bounding box for:

- Roads (`highway=*`) — centerlines with class tags for width/material
- Buildings (`building=*`) — footprint polygons with optional height/levels/type
- Land use (`landuse=*`, `natural=*`) — forest, residential, industrial, water, etc.
- Water (`waterway=*`, `natural=water`) — rivers, lakes, ponds
- Barriers (`barrier=*`) — walls, fences, hedges (NI has lots of stone walls)
- Place names (`place=*`) for level naming

Cache responses by bbox+query hash. Overpass rate-limits aggressively; the tool should respect their etiquette (Accept-Encoding, descriptive User-Agent, retry with backoff).

---

## 4. AI / smart components

MapNG-AI uses AI in two specific places. Both are small models, both run locally, both have CPU fallbacks. Everything else uses deterministic algorithms.

### 4.1 Land-cover semantic segmentation

The big visual win. Each pixel of the aerial imagery gets classified into a land-cover category. These classes drive both ground material assignment and foliage placement.

**Classes (target palette for NI):**

| ID | Class | Maps to material | Foliage rule |
|---|---|---|---|
| 0 | asphalt / road | Asphalt PBR | None |
| 1 | concrete / paving | Concrete PBR | None |
| 2 | short grass / lawn | Lush grass PBR | Grass billboards 2–5/m² |
| 3 | rough pasture | Rough grass PBR | Tufts + occasional gorse |
| 4 | bare earth / mud | Mud PBR | Sparse |
| 5 | gravel / track | Gravel PBR | None |
| 6 | water | Water shader | None |
| 7 | forest floor | Leaf litter PBR | Ferns + trees on top |
| 8 | rock / stone | Granite PBR | Moss decals only |
| 9 | crop field | Crop PBR (seasonal) | None |

BeamNG terrain supports up to 8 simultaneous material layers, so the most common classes for the area win. The tool collapses unused classes automatically.

**Model choice:**

- **Starter:** A pretrained aerial-segmentation model from Hugging Face (OpenEarthMap or LoveDA-trained UNet). Good 80% baseline, zero training time.
- **Upgrade path:** Fine-tune on ~100 hand-labelled NI patches in QGIS. A weekend's work, gets to ~95% accuracy on NI specifically.
- **Inference:** PyTorch with CUDA if available, ONNX Runtime CPU fallback. 2048² inference: ~1 s on RTX 3060, ~30 s on CPU.

### 4.2 Material splatting from class map

Not really AI — just smart processing of the segmentation output. The pipeline:

1. Take the class map (2048², integer-valued)
2. For each material class, generate a binary opacity mask
3. Gaussian-blur each mask slightly (~1.5 px sigma) so transitions feather naturally
4. Multiply by low-frequency Perlin noise to add organic variation (real grass has dirt patches; real dirt has tufts)
5. Hard-paint roads from OSM centerlines on top of the asphalt layer (OSM is more reliable than vision for roads)
6. Normalize all opacity layers so they sum to 1.0 per pixel
7. Write each opacity layer as a greyscale PNG that BeamNG references as a material layer

---

## 5. Object placement (buildings, foliage, etc.)

**Per spec: placeholder assets for v1, swap in real models or AI-generated ones later.** The placement system is designed so the asset source is irrelevant — it asks for "a residential building of roughly 12×8 m, 2 floors" and gets back a GLB. Whether that GLB is a coloured box, a CC0 model, or a freshly AI-generated mesh doesn't matter.

### 5.1 Asset provider interface

All asset providers implement the same Python protocol:

```python
from typing import Protocol
from pathlib import Path

class AssetProvider(Protocol):
    def get_building(
        self,
        footprint_m2: float,
        levels: int,
        building_type: str,  # 'residential' | 'commercial' | 'industrial' | etc.
        seed: int,
    ) -> Path:
        """Return path to a GLB file for this building."""
        ...

    def get_tree(self, species_hint: str, seed: int) -> Path: ...
    def get_foliage(self, foliage_type: str, seed: int) -> Path: ...

    def can_provide(self, asset_kind: str) -> bool:
        """Capability check for chained providers."""
        ...
```

**Three implementations ship out of the box:**

- **PlaceholderProvider** (default for v1) — generates simple boxes/cylinders procedurally. Buildings are extruded boxes coloured by type (residential = beige, commercial = grey-blue, industrial = grey, etc.). Trees are green cones on brown trunks. Ugly, fast, deterministic. **This is what v1 ships with.**
- **LibraryProvider** — reads from a local folder of GLB files organized by category, with a `manifest.json` describing each model's footprint, height, and floors. User drops CC0 models in here and they're picked up automatically.
- **APIProvider** — calls an external API (Meshy, CSM, Tripo, Rodin, custom) to generate buildings on demand. Caches results aggressively because generation is slow and expensive. Configurable timeout with fallback to PlaceholderProvider.

**Provider chaining:** Try APIProvider first, fall back to LibraryProvider, fall back to PlaceholderProvider. Each provider exposes its capability so the chain skips ones that can't satisfy a given request.

### 5.2 Placeholder asset spec (what v1 actually ships)

The PlaceholderProvider must produce GLB files procedurally at runtime. No bundled assets needed. Specs:

**Buildings:**
- Generate via `[trimesh.creation.box](http://trimesh.creation.box)`, scaled to requested footprint and `levels × 3 m` height
- Colour by type (use simple material, no textures):
  - residential: `#E8D5B7` (beige)
  - commercial: `#7A8DA0` (grey-blue)
  - industrial: `#6B6B6B` (grey)
  - retail: `#C49C7A` (sandy)
  - apartment: `#A8957C` (taupe)
  - garage / shed: `#8B7355` (brown)
  - default / unknown: `#999999` (medium grey)
- Add a slightly darker "roof" by inset-extruding the top face — keeps boxes from looking completely featureless when many are placed together

**Trees:**
- Cylinder trunk (brown, `#5D4037`) + cone canopy (green, `#2E7D32`)
- Height varies 6–15 m by seed
- Single GLB per call, deterministic from seed

**Foliage:**
- Single quad billboard with a procedurally-generated alpha-cutout texture (simple grass-blade pattern)
- One per call

### 5.3 Building placement algorithm

For each OSM building footprint:

1. Extract footprint polygon, compute oriented bounding box (centroid, length, width, rotation)
2. Determine building type: read `building=*` tag, or infer from context (nearest road class, containing landuse polygon, footprint area)
3. Determine target height: prefer `height` tag in metres, else `building:levels × 3 m`, else infer from type and area
4. Generate deterministic seed from OSM way ID — same map = same buildings every run
5. Ask the asset provider for a model matching `(area, levels, type, seed)`
6. Place at footprint centroid, snap Z to terrain elevation
7. Rotate to match OBB orientation (so long side of model = long side of footprint)
8. Scale uniformly to fit, **clamped to ±40%** of model's natural size — beyond that, accept slight footprint mismatch rather than distorting the model

**Edge cases the tool handles automatically:**

- Very large footprints (>2000 m²) — fall back to procedural box with a roof texture
- L-shaped or complex polygons — decompose into rectangles, place one model per rectangle
- Adjacent / touching footprints (terraces) — detect from OSM topology, prefer townhouse-row models if available
- Missing tags — context inference from surrounding landuse and roads

### 5.4 Foliage placement

Two layers, both deterministic:

- **Trees** — from OSM `landuse=forest` / `natural=wood` polygons. Poisson-disk sample positions inside each polygon (avoids regularity, avoids overlaps). Density depends on forest type tag if present, else default ~0.05 trees/m² (one tree per 20 m²). Species picked probabilistically — for NI: oak, ash, beech, sitka spruce in plantations, hawthorn at field edges.
- **Ground foliage** — from the segmentation class map. Each class has a foliage rule (see §4.1 table). Sampled at lower density than trees, billboards rather than meshes for performance.

---

## 6. Tech stack

| Layer | Technology | Why |
|---|---|---|
| App framework | Python 3.11+ with FastAPI | One process, async-friendly, geospatial libs are best in Python |
| Web UI | FastAPI-served HTML + Alpine.js or HTMX | No build step, runs on [localhost](http://localhost), dead simple |
| 3D preview | Three.js (in browser) | Industry standard; reuse MapNG approach |
| Map picker | Leaflet + leaflet-draw | Free, simple, works offline with cached tiles |
| Geospatial I/O | rasterio, shapely, geopandas, pyproj, mercantile | Standard Python geo stack |
| HTTP | httpx (async) | Parallel tile downloads |
| AI inference | PyTorch + ONNX Runtime | CUDA when available, CPU fallback |
| Mesh handling | trimesh, pygltflib | Read/write GLB for placeholders and library assets |
| BeamNG export | Custom (.ter writer) | Following BeamNG terrain binary spec |
| Packaging | uv or pip + pyproject.toml | Single install command |
| Optional Docker | Dockerfile + compose | For users who don't want to manage Python envs |

Total runtime dependencies: roughly 20 Python packages. No Redis, no Celery, no database. Cache lives in a local SQLite file.

---

## 7. Project structure

```
mapng-ai/
├── pyproject.toml
├── [README.md](http://README.md)
├── mapng_ai/
│   ├── __init__.py
│   ├── [app.py](http://app.py)                     # FastAPI entry
│   ├── [config.py](http://config.py)                  # Paths, API keys, defaults
│   │
│   ├── sources/                   # Stage 2: data fetching
│   │   ├── [base.py](http://base.py)                # ElevationSource / ImagerySource protocols
│   │   ├── [osni.py](http://osni.py)                # OSNI LiDAR + 10m DTM + ortho
│   │   ├── [terrarium.py](http://terrarium.py)           # AWS SRTM/Terrarium fallback
│   │   ├── [esri.py](http://esri.py)                # Esri World Imagery
│   │   ├── [overpass.py](http://overpass.py)            # OSM via Overpass API
│   │   └── [coverage.py](http://coverage.py)            # "Which source for this bbox?" logic
│   │
│   ├── pipeline/                  # Stages 1, 3, 5, 6, 7
│   │   ├── [region.py](http://region.py)              # CRS, tiles, bounds resolution
│   │   ├── [heightmap.py](http://heightmap.py)           # Stitch + fill + relax (ported from MapNG)
│   │   ├── [splatting.py](http://splatting.py)           # Class map → opacity layers
│   │   ├── [placement.py](http://placement.py)           # Buildings + foliage placement
│   │   ├── beamng_[ter.py](http://ter.py)          # .ter binary writer
│   │   └── beamng_[level.py](http://level.py)        # Full level package writer
│   │
│   ├── ai/                        # Stage 4
│   │   ├── [segmentation.py](http://segmentation.py)        # Land-cover model wrapper
│   │   └── models/                # Downloaded model weights (cached)
│   │
│   ├── assets/                    # Asset providers
│   │   ├── [base.py](http://base.py)                # AssetProvider protocol
│   │   ├── [placeholder.py](http://placeholder.py)         # Procedural box generator
│   │   ├── [library.py](http://library.py)             # GLB folder reader
│   │   └── api_[provider.py](http://provider.py)        # External AI API client
│   │
│   ├── cache/                     # Persistent caches (tiles, models, generated)
│   └── ui/                        # Static HTML/JS/CSS for web UI
│
├── assets/                        # USER-PROVIDED — empty initially
│   ├── buildings/
│   │   ├── residential/
│   │   ├── commercial/
│   │   ├── industrial/
│   │   └── _fallback/
│   ├── trees/
│   ├── foliage/
│   └── materials/                 # PBR ground textures
│
├── output/                        # Generated levels
└── tests/
```

---

## 8. Build plan

Phased so each phase produces something runnable and testable. Don't skip ahead — earlier phases catch bugs that compound later. Estimates assume part-time evenings/weekends.

### Phase 0 — Skeleton (3–5 days)

- [ ] Set up Python project with `pyproject.toml`, dev environment, basic FastAPI app serving a hello-world page
- [ ] Web UI with a Leaflet map, draw-rectangle tool, "Generate" button that POSTs the bounding box to the backend
- [ ] Stub pipeline that just logs each stage. No real processing yet
- [ ] WebSocket or SSE channel for streaming progress updates back to the UI
- [ ] End-to-end smoke test: click on map, see logs progress in browser

### Phase 1 — Real elevation data (4–6 days)

- [ ] Implement OSNI LiDAR fetcher with coverage index lookup
- [ ] Implement Terrarium fallback fetcher
- [ ] Coverage-router: given a bbox, picks the best available source
- [ ] Heightmap stitcher with NoData fill and bilaplacian relaxation (port the MapNG `functions.txt` algorithm — `expandFill` + `relaxFilled`)
- [ ] Reprojection from Irish Grid → working CRS via `rasterio.warp`
- [ ] Output: a single GeoTIFF + 16-bit PNG heightmap. Open in QGIS to verify it looks right

### Phase 2 — BeamNG export (4–6 days) ⭐ GATE

- [ ] `.ter` binary writer following BeamNG's terrain format spec
- [ ] Minimal level package writer: directory structure, `main.level.json`, terrain reference, skybox, sun, single default material
- [ ] ZIP packaging with proper internal paths
- [ ] **Test: generate a level for a small NI area, drop it in BeamNG's `levels/` folder, verify it loads and you can drive on it**

**This is the gate. Don't move on until you can drive on a generated level. Everything after this is improvements.**

### Phase 3 — OSM + placeholder buildings (3–5 days)

- [ ] Overpass API client with caching
- [ ] PlaceholderProvider — procedural coloured boxes per building type (see §5.2)
- [ ] Building placement algorithm (OBB extraction, type inference, scaling, rotation, terrain-snap — see §5.3)
- [ ] Write building placements into the level package
- [ ] Test: generate a level of central Belfast, verify buildings appear in roughly the right spots and orientations

### Phase 4 — Imagery + segmentation + materials (1–2 weeks)

- [ ] OSNI ortho fetcher + Esri fallback
- [ ] Pretrained segmentation model integration (download weights on first run, cache locally)
- [ ] Class map → opacity layer pipeline (blur, noise, OSM road overlay)
- [ ] Material library: ship with maybe 6 default PBR materials (asphalt, grass, dirt, gravel, concrete, water)
- [ ] Wire opacity layers into BeamNG level package

This is the biggest visual upgrade. Worth taking time over.

### Phase 5 — Foliage + polish (1 week)

- [ ] Placeholder tree provider (green cones)
- [ ] Poisson-disk sampling in OSM forest polygons
- [ ] Class-driven ground foliage scattering
- [ ] Water shader on water polygons
- [ ] Spawn point heuristic (pick a flat spot near a road, not in a lake)
- [ ] Decal road support for lane markings

### Phase 6 — Asset providers (ongoing)

- [ ] LibraryProvider — read GLB folder with manifest. Lets user drop in CC0 models from Quaternius, Kenney, etc.
- [ ] APIProvider scaffolding — generic client for AI mesh generation services with caching
- [ ] UI for managing asset libraries and API credentials

### Phase 7 — Quality of life (ongoing)

- [ ] Batch jobs for tile grids (matches MapNG's batch feature)
- [ ] Resume support for failed jobs
- [ ] Save/restore session files (`.mapng-ai` equivalent)
- [ ] Docker image
- [ ] Self-test command that verifies all data sources are reachable

---

## 9. Realistic time estimate

| Phase | Effort (part-time) | Drivable result? |
|---|---|---|
| 0 – Skeleton | 3–5 days | No |
| 1 – Elevation | 4–6 days | No |
| 2 – BeamNG export | 4–6 days | Yes — flat-textured terrain only |
| 3 – OSM + placeholders | 3–5 days | Yes — recognizable layout |
| 4 – Segmentation + materials | 1–2 weeks | Yes — real-looking ground |
| 5 – Foliage + polish | 1 week | Yes — close to v1 quality |
| 6 – Asset providers | ongoing | — |
| 7 – QoL | ongoing | — |
| **Total to v1** | **~6–8 weeks** | — |

First playable result (drivable level, no buildings, flat texture) at end of Phase 2 — about 2 weeks in.

---

## 10. Locked decisions (2026-04-29)

- **Target hardware.** RTX 4070 Super, 32 GB RAM, Ryzen 7 5800X3D. AI inference comfortably fits on GPU; segmentation will run in ~1 s for 2048².
- **Default test area.** 2×2 km rural area near Cookstown, Northern Ireland.
- **LiDAR coverage strategy.** Take whatever OSNI exposes for the bbox; assume 10 m DTM as worst case for rural NI. Don't pre-check coverage; let the source-router pick.
- **Working CRS.** Irish Transverse Mercator (EPSG:2157). OSNI sources land in this directly; OSM (EPSG:4326) and Esri (EPSG:3857) get reprojected.
- **Package manager.** `uv`.
- **Repo layout.** Keep folder name `Beamng Mapping Project`; Python package at `./mapng_ai/`.
- **Phase 0 deliverable.** Web UI from day one — Leaflet bbox picker + Three.js preview pane + SSE progress feed. No CLI MVP.
- **Web preview is the iteration loop.** The Three.js preview pane is upgraded every phase (heightmap mesh in P1, terrain + buildings in P3, materials in P4, foliage in P5) so the user can iterate without launching BeamNG. BeamNG is verification-only.
- **BeamNG installation.** User installs generated levels manually into BeamNG's `levels/` folder for verification. The tool only writes the ZIP.
- **Distribution.** TBD (revisit at Phase 6).
- **AI building generation API.** TBD (revisit at Phase 6).

---

## 11. Things deliberately NOT in v1

Stating these explicitly so they don't sneak back in as scope creep:

- ❌ Terrain super-resolution AI. Not needed because OSNI gives native 1 m for most of NI. Add later if needed for areas with poor DEM coverage.
- ❌ Per-building AI generation. Placeholder + library + API plug-in covers it; per-building diffusion is an order of magnitude slower and not worth the complexity.
- ❌ Photogrammetric building reconstruction from street view. Cool, slow, hard, unreliable. Maybe v2.
- ❌ Cloud backend / multi-user. Local-first is the design.
- ❌ Real-time editing. Generate-then-edit-in-BeamNG-Editor is the workflow.
- ❌ Non-BeamNG export targets. Could add Unreal/Unity/MSFS later but not now.

---

## 12. Implementation notes for Claude Code

When building this:

1. **Start by reading the MapNG repo** (github.com/nikkiluzader/mapng) to understand the predecessor. The `functions.txt` file contains the heightmap fill+relax algorithm worth porting directly to Python. The README documents the BeamNG `.ter` format references.

2. **Reference docs to consult during implementation:**
   - BeamNG terrain `.ter` format: search BeamNG documentation / forum threads
   - BeamNG level package structure: examine an existing official level (e.g. `west_coast_usa`) by unpacking it
   - OSNI dataset endpoints: [opendatani.gov.uk](http://opendatani.gov.uk) dataset pages (they vary per dataset)
   - Overpass API query syntax: [overpass-turbo.eu](http://overpass-turbo.eu) for testing queries before coding them
   - Hugging Face for pretrained aerial segmentation models

3. **Phase 2 is the gate.** Don't skip ahead. A level that loads in BeamNG and lets you drive proves the export format is correct. Every later phase is a quality upgrade on this foundation.

4. **The asset provider abstraction matters.** Build it as a Protocol from Phase 3 onwards. This lets the user swap placeholder boxes for CC0 library models, then for an AI generation API, with a config change rather than a refactor.

5. **Determinism is important.** Every random choice (asset selection, foliage position, etc.) must be seeded from stable inputs (OSM IDs, coordinates). Re-running on the same bbox must produce the same output.

6. **Cache aggressively.** Tile downloads, OSM queries, model inference results, generated assets. Use a local SQLite for cache metadata, files on disk for content. Cache invalidation is explicit (a CLI command), not automatic.

7. **Progress streaming.** Each pipeline stage should emit progress events. The UI shows real-time progress; this also makes debugging long-running pipelines much easier.

8. **Honest error messages.** If OSNI is down, say so. If the bbox is outside any LiDAR coverage, say what fallback is being used. Never fail silently.

---

*End of spec — v0.1*

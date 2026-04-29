# MapNG-AI

Locally-run tool that generates [BeamNG.drive](https://www.beamng.com/) maps from real-world locations.

> **Status:** Phase 0 (skeleton). See [docs/SPEC.md](docs/SPEC.md) for full design.

## Quick start

```bash
# install uv if you don't already have it
python -m pip install --user uv

# install dependencies (Phase 0 = base + dev only)
python -m uv sync

# run the app
python -m uv run python -m mapng_ai
```

Then open http://127.0.0.1:8000/ — draw a bounding box on the map, click Generate, watch progress stream.

## What works right now (Phase 0)

- Web UI with Leaflet bbox picker
- Three.js preview pane (placeholder; grows in later phases)
- Server-Sent-Events progress feed
- Stub 7-stage pipeline (logs only — no real data fetched yet)

## What's coming

See [docs/SPEC.md §8](docs/SPEC.md) for the phased plan. Phase 1 = real OSNI elevation. Phase 2 = drivable BeamNG export.

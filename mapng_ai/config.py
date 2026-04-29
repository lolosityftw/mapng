from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

PACKAGE_DIR = ROOT / "mapng_ai"
UI_DIR = PACKAGE_DIR / "ui"
STATIC_DIR = UI_DIR / "static"
TEMPLATES_DIR = UI_DIR / "templates"

CACHE_DIR = PACKAGE_DIR / "cache"
MODELS_DIR = PACKAGE_DIR / "ai" / "models"
OUTPUT_DIR = ROOT / "output"

WORKING_CRS = "EPSG:2157"  # Irish Transverse Mercator

DEFAULT_HEIGHTMAP_SIZE = 2048
DEFAULT_BBOX_SIZE_M = 2000

HOST = "127.0.0.1"
PORT = 8000


def ensure_runtime_dirs() -> None:
    for d in (CACHE_DIR, MODELS_DIR, OUTPUT_DIR):
        d.mkdir(parents=True, exist_ok=True)

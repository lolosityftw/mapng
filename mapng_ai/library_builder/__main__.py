"""CLI: `python -m mapng_ai.library_builder build [--only=building,tree,vehicle]`

Loads .env (so the Meshy key is picked up), runs the batch, prints progress.
"""
from __future__ import annotations

import asyncio
import os
import sys

from mapng_ai import config


# Load .env before importing anything that reads env vars
_dotenv = config.ROOT / ".env"
if _dotenv.exists():
    for line in _dotenv.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


from mapng_ai.library_builder.runner import build_library, library_status


async def _emit(event: str, data: dict) -> None:
    if event == "entry:start":
        print(f"  start    {data['slug']:<28} ({data['category']}/{data['type']})", flush=True)
    elif event == "entry:done":
        print(f"  done     {data['slug']:<28} {data['size_bytes']/1e6:.1f} MB"
              f"   [{data['completed']}/{data['total']}]", flush=True)
    elif event == "entry:skip":
        print(f"  skip     {data['slug']:<28} (already cached)", flush=True)
    elif event == "entry:fail":
        print(f"  FAIL     {data['slug']}", flush=True)
    elif event == "batch:start":
        print(f"\nbatch start: {data['total']} entries  (categories: {data['categories']})\n", flush=True)
    elif event == "batch:done":
        print(f"\nbatch done: {data['completed']} processed, "
              f"{data['skipped']} skipped, {data['failed']} failed", flush=True)


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in ("build", "status"):
        print("usage: python -m mapng_ai.library_builder {build|status} [--only=building,tree,vehicle]")
        return 1

    if argv[0] == "status":
        s = library_status()
        print("library status:")
        for cat, types in s["by_category"].items():
            n = s["totals"][cat]
            print(f"  {cat:10}  {n:>3} GLBs   ({types})")
        return 0

    only = None
    for a in argv[1:]:
        if a.startswith("--only="):
            only = [x.strip() for x in a.split("=", 1)[1].split(",") if x.strip()]

    progress = asyncio.run(build_library(categories=only, emit=_emit))
    return 0 if progress.failed == 0 else 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

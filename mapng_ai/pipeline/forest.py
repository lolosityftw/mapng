"""BeamNG Forest object generation — replaces tree TSStatics with the
native Forest system for massive perf wins (~10× more trees with no FPS hit).

Reference: vanilla Italy uses:
  - `levels/<lvl>/art/forest/managedItemData.json` — TSForestItemData entries
    (one per species — references the DAE shape and wind/sway params)
  - `levels/<lvl>/forest/<species>.forest4.json` — NDJSON, one line per
    placed instance: {pos, rotationMatrix, scale, type}
  - One `Forest` scene object (named "theForest") inside a vegetation SimGroup.

We split foliage into two paths:
  - TREES (species != "bush") → Forest system
  - BUSHES (species == "bush") → TSStatics (unchanged — only ~2k of them,
    they're short, and the Forest system's wind/sway is overkill for bushes)
"""
from __future__ import annotations

import json
import math
import uuid
from typing import Sequence


def _stable_uuid(seed: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"mapng:forest:{seed}"))


def tree_managed_item_data(level_name: str) -> dict:
    """Build the `art/forest/managedItemData.json` content.

    For our MVP we have a single tree species (the placeholder DAE).
    When we add more tree variants, they each get an entry here.
    """
    pid = _stable_uuid(f"{level_name}:MapNG_tree")
    return {
        "MapNG_tree": {
            "name": "MapNG_tree",
            "internalName": "MapNG_tree",
            "class": "TSForestItemData",
            "persistentId": pid,
            "annotation": "NATURE",
            "shapeFile": f"levels/{level_name}/art/shapes/foliage/tree.dae",
            # Wind sway parameters (cloned from Italy's cork_oak_medium)
            "branchAmp":          0.10,
            "detailAmp":          0.20,
            "detailFreq":         0.5,
            "tightnessCoefficient": 1.0,
            "trunkBendScale":     0.05,
            "windScale":          0.5,
        }
    }


def trees_forest4_lines(trees: Sequence) -> list[str]:
    """Convert tree placements (excluding bushes) into NDJSON lines for
    `forest/MapNG_tree.forest4.json`.

    Forest items have a single UNIFORM scale — we use max(scale_xyz) so
    the tree's height isn't squashed. Position + rotation match the
    TSStatic format exactly (3×3 row-major rotation matrix as 9 floats).
    """
    lines: list[str] = []
    for t in trees:
        species = getattr(t, "species", "default") or "default"
        if species == "bush":
            continue   # bushes stay as TSStatics
        sx, sy, sz = t.scale_xyz
        scale = max(sx, sy, sz)
        c, s = math.cos(t.yaw), math.sin(t.yaw)
        rot = [c, -s, 0, s, c, 0, 0, 0, 1]
        line = {
            "ctxid": 0,
            "pos":   [round(t.x, 3), round(t.y, 3), round(t.z, 3)],
            "rotationMatrix": rot,
            "scale": round(scale, 3),
            "type":  "MapNG_tree",
        }
        lines.append(json.dumps(line, separators=(",", ":")))
    return lines


def split_trees_and_bushes(trees: Sequence) -> tuple[list, list]:
    """Return (forest_trees, bush_tsstatics)."""
    forest_trees = []
    bushes = []
    for t in trees:
        if getattr(t, "species", None) == "bush":
            bushes.append(t)
        else:
            forest_trees.append(t)
    return forest_trees, bushes


def forest_scene_objects(level_name: str) -> list[dict]:
    """SimGroup contents for the Forest object in items.level.json."""
    return [
        {
            "name": "theForest",
            "class": "Forest",
            "lodReflectScalar": 0,
        },
    ]

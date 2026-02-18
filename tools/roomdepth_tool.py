"""
check_room_depth – Room Depth vs. Daylight Penetration check.

Regulation reference:
    BS 8206-2 / EN 17037 rule of thumb – usable daylight penetration
    ≈ 2.5 × the window head height.  Rooms deeper than this risk
    insufficient natural light at the back.

Discovered by the orchestrator as ``check_room_depth``.
"""

import ifcopenshell
import ifcopenshell.util.element as element_util
from typing import Any, Dict, List, Optional

from .ifc_helpers import (
    get_psets,
    get_space_dimensions,
    get_windows_in_space,
    max_window_head_height,
)

# Spaces whose names match these keywords are skipped by default because
# the daylight-penetration rule applies to *habitable* rooms only.
_NON_HABITABLE_KEYWORDS = {
    "hallway", "corridor", "stair", "staircase", "foyer", "lobby",
    "utility", "storage", "closet", "shaft", "roof", "riser",
    "mechanical", "electrical", "elevator", "lift", "WC",
}

TOOL_SCHEMA = {
    "name": "check_room_depth",
    "description": (
        "Check whether room depth exceeds the daylight penetration limit "
        "(depth_factor × window head height) in an IFC building model. "
        "Regulation: BS 8206-2 / EN 17037."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "ifc_file_path": {
                "type": "string",
                "description": "Path to the IFC model file",
            },
            "depth_factor": {
                "type": "number",
                "description": "Maximum room depth as multiple of window head height (default 2.5)",
            },
            "room_types": {
                "type": "string",
                "description": (
                    "Comma-separated room types to check, e.g. 'Bedroom,Living Room'. "
                    "If omitted, all habitable rooms are checked "
                    "(non-habitable spaces like Hallway, Stair, Utility are skipped)."
                ),
            },
        },
        "required": ["ifc_file_path"],
    },
}


def _is_habitable(name: str) -> bool:
    """Return *False* for names that match non-habitable space keywords."""
    lower = name.lower()
    return not any(kw in lower for kw in _NON_HABITABLE_KEYWORDS)


def check_room_depth(
    ifc_file_path: str,
    depth_factor: float = 2.5,
    room_types: Optional[str] = None,
) -> Dict[str, Any]:
    """Check whether room depth exceeds the daylight penetration limit.

    Args:
        ifc_file_path: Path to the IFC model file.
        depth_factor: Max depth as multiple of window head height (default 2.5).
        room_types: Comma-separated room types to check.  When omitted,
                    all **habitable** rooms are checked (non-habitable
                    spaces are skipped automatically).

    Returns:
        Dictionary with per-room results and overall compliance status.
    """
    try:
        ifc_file = ifcopenshell.open(ifc_file_path)
    except OSError:
        return {"error": f"Cannot open IFC file at '{ifc_file_path}'"}

    filter_list = [r.strip() for r in room_types.split(",")] if room_types else None
    results: List[Dict[str, Any]] = []
    skipped: List[str] = []

    for space in ifc_file.by_type("IfcSpace"):
        long_name = space.LongName or space.Name or ""

        # --- Explicit room-type filter ---
        if filter_list:
            if not any(rt.lower() in long_name.lower() for rt in filter_list):
                continue
        else:
            # --- Default: skip non-habitable spaces ---
            if not _is_habitable(long_name):
                skipped.append(long_name)
                continue

        dims = get_space_dimensions(space)
        depth = dims["depth"]

        space_windows = get_windows_in_space(space, ifc_file)
        win_head_h = max_window_head_height(space_windows)

        if win_head_h <= 0:
            results.append({
                "room": long_name,
                "room_id": space.Name or "",
                "depth_m": round(depth, 3),
                "window_head_height_m": 0,
                "max_allowed_depth_m": 0,
                "pass": False,
                "note": "No windows found for this space",
            })
            continue

        max_depth = depth_factor * win_head_h
        results.append({
            "room": long_name,
            "room_id": space.Name or "",
            "depth_m": round(depth, 3),
            "window_head_height_m": round(win_head_h, 3),
            "max_allowed_depth_m": round(max_depth, 3),
            "pass": depth <= max_depth,
        })

    non_compliant = [r["room"] for r in results if not r["pass"]]
    return {
        "results": results,
        "rooms_checked": len(results),
        "non_compliant_rooms": non_compliant,
        "skipped_non_habitable": skipped,
        "overall_pass": len(non_compliant) == 0,
    }

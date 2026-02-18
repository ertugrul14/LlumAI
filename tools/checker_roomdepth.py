"""
check_room_depth – Room Depth vs. Daylight Penetration check.

Regulation reference:
    BS 8206-2 / EN 17037 rule of thumb – usable daylight penetration
    ≈ 2.5 × the window head height.  Rooms deeper than this risk
    insufficient natural light at the back.

Standalone module – no external local dependencies.
Only requires: ifcopenshell (pip install ifcopenshell)
"""

import math
import ifcopenshell
import ifcopenshell.util.element as element_util
from typing import Any, Dict, List, Optional, Union


# ═══════════════════════════════════════════════════════════════════════════
# Inline IFC helpers (self-contained – no ifc_helpers.py dependency)
# ═══════════════════════════════════════════════════════════════════════════

def _get_psets(element) -> Dict[str, Dict[str, Any]]:
    """Return ``{pset_name: {prop: value}}`` for an IFC element."""
    try:
        return element_util.get_psets(element)
    except Exception:
        psets: Dict[str, Dict[str, Any]] = {}
        for rel in getattr(element, "IsDefinedBy", []):
            if rel.is_a("IfcRelDefinesByProperties"):
                pset = rel.RelatingPropertyDefinition
                if pset.is_a("IfcPropertySet"):
                    props: Dict[str, Any] = {}
                    for p in pset.HasProperties:
                        if p.is_a("IfcPropertySingleValue") and p.NominalValue:
                            props[p.Name] = p.NominalValue.wrappedValue
                    psets[pset.Name] = props
        return psets


def _get_quantity_value(element, q_names: Union[str, List[str]]) -> Optional[float]:
    """Get a quantity value from ``IfcElementQuantity`` sets."""
    if isinstance(q_names, str):
        q_names = [q_names]
    for rel in getattr(element, "IsDefinedBy", []):
        if rel.is_a("IfcRelDefinesByProperties"):
            qset = rel.RelatingPropertyDefinition
            if qset.is_a("IfcElementQuantity"):
                for q in qset.Quantities:
                    if q.Name in q_names:
                        for attr in ("AreaValue", "LengthValue", "VolumeValue"):
                            val = getattr(q, attr, None)
                            if val is not None:
                                return val
    return None


def _get_windows_in_space(space, ifc_file) -> list:
    """Find windows associated with a space via ``IfcRelSpaceBoundary``."""
    windows = []
    for rel in ifc_file.by_type("IfcRelSpaceBoundary"):
        if rel.RelatingSpace == space:
            elem = rel.RelatedBuildingElement
            if elem and elem.is_a("IfcWindow"):
                windows.append(elem)
    return windows


def _get_space_dimensions(space) -> Dict[str, float]:
    """Estimate room length, width, height, and depth from quantities / psets.

    Tries direct Length/Width quantities first.  When those are absent (common
    in Revit-exported IFC), falls back to estimating dimensions from
    ``Area`` + ``Perimeter`` by solving the rectangle formula:

        L + W = P/2,  L * W = A  →  quadratic
    """
    length = _get_quantity_value(space, ["Length"])
    width = _get_quantity_value(space, ["Width"])
    height = _get_quantity_value(space, ["Height", "ClearHeight", "FinishCeilingHeight"])
    area: Optional[float] = None
    perimeter: Optional[float] = None

    psets = _get_psets(space)
    for pn, pv in psets.items():
        if isinstance(pv, dict):
            if not length:
                length = pv.get("Length", 0) or 0
            if not width:
                width = pv.get("Width", 0) or 0
            if not height:
                height = (
                    pv.get("Height", 0)
                    or pv.get("Unbounded Height", 0)
                    or pv.get("UnboundedHeight", 0)
                    or pv.get("ClearHeight", 0)
                ) or 0
            if not area:
                area = pv.get("Area", 0) or pv.get("NetFloorArea", 0) or 0
            if not perimeter:
                perimeter = pv.get("Perimeter", 0) or 0

    # Also try IfcElementQuantity for area/perimeter
    if not area:
        area = _get_quantity_value(space, ["NetFloorArea", "GrossFloorArea"]) or 0
    if not perimeter:
        perimeter = _get_quantity_value(space, ["GrossPerimeter", "Perimeter"]) or 0

    # Fallback: estimate length/width from Area + Perimeter (rectangle model)
    if (not length or not width) and area and perimeter:
        half_p = perimeter / 2
        disc = half_p ** 2 - 4 * area
        if disc >= 0:
            sqrt_disc = math.sqrt(disc)
            length = (half_p + sqrt_disc) / 2
            width = (half_p - sqrt_disc) / 2

    return {
        "length": length or 0,
        "width": width or 0,
        "height": height or 0,
        "area": area or 0,
        "perimeter": perimeter or 0,
        "depth": max(length or 0, width or 0),
    }


def _read_window_head_height(window) -> float:
    """Best-effort head-height for a single window."""
    psets = _get_psets(window)
    sill: Optional[float] = None
    head: Optional[float] = None

    for pn, pv in psets.items():
        if not isinstance(pv, dict):
            continue
        # Direct head height (Revit "Head Height")
        for key in ("Head Height", "HeadHeight"):
            val = pv.get(key)
            if isinstance(val, (int, float)) and val > 0:
                head = val
        # Sill height
        for key in ("Sill Height", "SillHeight"):
            val = pv.get(key)
            if isinstance(val, (int, float)) and val >= 0:
                sill = val

    # 1. Prefer explicit head height
    if head is not None and head > 0:
        return head
    # 2. Sill + window height
    h = window.OverallHeight or 0
    if sill is not None:
        return sill + h
    # 3. Fallback: assume sill ≈ 0.9 m
    return 0.9 + h if h > 0 else 0


def _max_window_head_height(windows) -> float:
    """Get the maximum window head height above the floor."""
    if not windows:
        return 0
    max_h = 0.0
    for w in windows:
        head_h = _read_window_head_height(w)
        if head_h and head_h > max_h:
            max_h = head_h
    return max_h


# ═══════════════════════════════════════════════════════════════════════════
# Tool schema & main check function
# ═══════════════════════════════════════════════════════════════════════════

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
    model,
    depth_factor: float = 2.5,
    room_types: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Check whether room depth exceeds the daylight penetration limit.

    Args:
        model: An ifcopenshell.file object (already opened).
        depth_factor: Max depth as multiple of window head height (default 2.5).
        room_types: Comma-separated room types to check.  When omitted,
                    all **habitable** rooms are checked (non-habitable
                    spaces are skipped automatically).

    Returns:
        list[dict] — one dict per element, maps to element_results DB rows.
    """
    ifc_file = model

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

        dims = _get_space_dimensions(space)
        depth = dims["depth"]

        space_windows = _get_windows_in_space(space, ifc_file)
        win_head_h = _max_window_head_height(space_windows)

        if win_head_h <= 0:
            results.append({
                "element_id":        space.GlobalId,
                "element_type":      "IfcSpace",
                "element_name":      space.Name or "",
                "element_name_long": long_name,
                "check_status":      "blocked",
                "actual_value":      f"depth={round(depth, 3)} m",
                "required_value":    None,
                "comment":           "No windows found for this space",
                "log":               None,
            })
            continue

        max_depth = depth_factor * win_head_h
        passed = depth <= max_depth
        results.append({
            "element_id":        space.GlobalId,
            "element_type":      "IfcSpace",
            "element_name":      space.Name or "",
            "element_name_long": long_name,
            "check_status":      "pass" if passed else "fail",
            "actual_value":      f"depth={round(depth, 3)} m",
            "required_value":    f"<= {round(max_depth, 3)} m ({depth_factor} x {round(win_head_h, 3)} m head height)",
            "comment":           f"window_head_height_m={round(win_head_h, 3)}",
            "log":               None,
        })

    n_fail = sum(1 for r in results if r["check_status"] == "fail")
    n_blocked = sum(1 for r in results if r["check_status"] == "blocked")

    if n_fail == 0 and n_blocked == 0:
        summary = f"All {len(results)} habitable rooms pass room depth check (factor {depth_factor})."
    elif n_fail == 0:
        summary = f"All rooms with windows pass. {n_blocked} room(s) have no windows (blocked)."
    else:
        summary = (
            f"{n_fail} room(s) exceed max depth. {n_blocked} room(s) have no windows. "
            f"Factor: {depth_factor}x window head height."
        )
    return results

"""
check_shading – Shading Device Presence on critical facades.

Standard references:
  • IFC shading-device detection (IfcShadingDevice IFC4+,
    IfcBuildingElementProxy / IfcPlate with shading keywords for IFC2X3)
  • Property-set shading flags (HasShading, ExternalShading, etc.)

Standalone module – no external local dependencies.
Only requires: ifcopenshell (pip install ifcopenshell)
"""

import math
import ifcopenshell
import ifcopenshell.util.element as element_util
from typing import Any, Dict, List, Optional, Tuple, Union


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


def _angle_from_vector(dx: float, dy: float) -> float:
    """Return bearing in degrees (0=North, clockwise) from a 2-D direction."""
    return math.degrees(math.atan2(dx, dy)) % 360


def _cardinal(angle_deg: float) -> str:
    """Map an angle (0=North, clockwise) to a cardinal/inter-cardinal label."""
    dirs = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
    idx = int(((angle_deg + 22.5) % 360) / 45)
    return dirs[idx]


def _simple_cardinal(card: str) -> str:
    """Simplify an inter-cardinal label to one of N/S/E/W."""
    if card in ("N", "S", "E", "W"):
        return card
    return card[0] if card[0] in ("N", "S", "E", "W") else card


def _get_true_north(ifc_file) -> float:
    """Extract true-north angle from ``IfcGeometricRepresentationContext``."""
    for ctx in ifc_file.by_type("IfcGeometricRepresentationContext"):
        tn = getattr(ctx, "TrueNorth", None)
        if tn:
            dx, dy = tn.DirectionRatios[0], tn.DirectionRatios[1]
            return _angle_from_vector(dx, dy)
    return 0.0


def _wall_direction(wall) -> Optional[Tuple[float, float]]:
    """Extract wall direction vector ``(dx, dy)`` from its ``ObjectPlacement``.

    Per the IFC spec, when ``RefDirection`` is absent on an
    ``IfcAxis2Placement3D`` the default local X-axis is ``(1, 0, 0)``.
    """
    try:
        placement = wall.ObjectPlacement
        if placement and placement.is_a("IfcLocalPlacement"):
            rel = placement.RelativePlacement
            if rel and rel.is_a("IfcAxis2Placement3D"):
                ref_dir = rel.RefDirection
                if ref_dir:
                    return (ref_dir.DirectionRatios[0], ref_dir.DirectionRatios[1])
                # IFC default: local X-axis = (1, 0, 0)
                return (1.0, 0.0)
    except Exception:
        pass
    return None


def _is_external(wall) -> bool:
    """Check if a wall is marked as external in its property sets."""
    psets = _get_psets(wall)
    for pset_name, props in psets.items():
        if isinstance(props, dict) and props.get("IsExternal") is True:
            return True
    return False


def _find_host_wall(window, ifc_file):
    """Find the host wall for a window via ``IfcRelFillsElement`` → ``IfcRelVoidsElement``."""
    for rel in ifc_file.by_type("IfcRelFillsElement"):
        if rel.RelatedBuildingElement == window:
            opening = rel.RelatingOpeningElement
            for rel2 in ifc_file.by_type("IfcRelVoidsElement"):
                if rel2.RelatedOpeningElement == opening:
                    host = rel2.RelatingBuildingElement
                    if host.is_a("IfcWall") or host.is_a("IfcWallStandardCase"):
                        return host
            break
    return None


def _is_shading_entity(elem) -> bool:
    """True if *elem* is a shading device or a proxy/plate named like one."""
    try:
        if elem.is_a("IfcShadingDevice"):
            return True
    except Exception:
        pass
    if elem.is_a("IfcBuildingElementProxy") or elem.is_a("IfcPlate") or elem.is_a("IfcMember"):
        name_lower = ((elem.Name or "") + " " + (getattr(elem, "ObjectType", "") or "")).lower()
        _kws = ("shade", "shading", "sunshade", "brise", "louver", "louvre",
                "blind", "fin", "overhang", "canopy", "awning", "screen", "slat")
        if any(kw in name_lower for kw in _kws):
            return True
    return False


def _find_shading_for_window(window, ifc_file) -> bool:
    """Check if a window has an associated shading device.

    Detection paths (in order):
    1. Inverse ``ConnectedTo`` / ``ConnectedFrom`` for shading-device links
    2. ``IsDecomposedBy`` — child shading elements
    3. ``IfcRelConnectsElements`` scan for IfcShadingDevice (IFC4+)
    4. Property-set flags (shad/blind/louver/solar keywords)
    """
    # 1. Inverse relationships on the window (fast, no model-wide scan)
    for rel in getattr(window, "ConnectedTo", []):
        related = getattr(rel, "RelatedElement", None)
        if related and _is_shading_entity(related):
            return True
    for rel in getattr(window, "ConnectedFrom", []):
        relating = getattr(rel, "RelatingElement", None)
        if relating and _is_shading_entity(relating):
            return True

    # 2. Decomposition — shading as child of window
    for rel in getattr(window, "IsDecomposedBy", []):
        for obj in getattr(rel, "RelatedObjects", []):
            if _is_shading_entity(obj):
                return True

    # 3. IFC4+: check IfcShadingDevice via IfcRelConnectsElements
    try:
        shading_devices = ifc_file.by_type("IfcShadingDevice")
        if shading_devices:
            sd_ids = {sd.id() for sd in shading_devices}
            for rel in getattr(window, "ConnectedTo", []):
                if getattr(rel, "RelatedElement", None) and rel.RelatedElement.id() in sd_ids:
                    return True
            for rel in getattr(window, "ConnectedFrom", []):
                if getattr(rel, "RelatingElement", None) and rel.RelatingElement.id() in sd_ids:
                    return True
    except RuntimeError:
        pass  # IfcShadingDevice not in schema (IFC2X3)

    # 4. Check property sets for shading-related flags
    psets = _get_psets(window)
    for pn, pv in psets.items():
        if isinstance(pv, dict):
            for key, val in pv.items():
                kl = key.lower()
                if any(kw in kl for kw in (
                    "shad", "blind", "louver", "louvre", "solar",
                    "screen", "awning", "overhang", "brise",
                    "hasshading", "externalshading", "protectionsolar",
                )):
                    if val and str(val).lower() not in ("", "none", "false", "0", "no"):
                        return True

    return False


# ═══════════════════════════════════════════════════════════════════════════
# Shading index for IFC2X3 / non-standard models
# ═══════════════════════════════════════════════════════════════════════════

_SHADING_KEYWORDS = frozenset([
    "shade", "shading", "sunshade", "sunscreen", "brise", "brise-soleil",
    "louver", "louvre", "blind", "fin", "overhang", "canopy", "awning",
    "solar", "screen", "slat",
])


def _build_shading_index(ifc_file) -> Dict[int, list]:
    """Build an index of potential shading elements per hosting wall.

    For IFC2X3, ``IfcShadingDevice`` doesn't exist, so we also look for
    ``IfcBuildingElementProxy``, ``IfcPlate``, ``IfcMember`` whose names or
    type names contain shading keywords.

    Returns ``{wall_id: [shading_elements]}`` for quick per-window lookup.
    """
    shading_elems: list = []

    # IFC4+: direct IfcShadingDevice
    try:
        shading_elems.extend(ifc_file.by_type("IfcShadingDevice"))
    except RuntimeError:
        pass

    # IFC2X3 fallback: proxy / plate / member with shading-like names
    for type_name in ("IfcBuildingElementProxy", "IfcPlate", "IfcMember"):
        try:
            for elem in ifc_file.by_type(type_name):
                name_lower = ((elem.Name or "") + " " + (getattr(elem, "ObjectType", "") or "")).lower()
                if any(kw in name_lower for kw in _SHADING_KEYWORDS):
                    shading_elems.append(elem)
        except RuntimeError:
            pass

    # Also check property-set-flagged elements
    for type_name in ("IfcBuildingElementProxy", "IfcPlate"):
        try:
            for elem in ifc_file.by_type(type_name):
                if elem in shading_elems:
                    continue
                psets = _get_psets(elem)
                for pn, pv in psets.items():
                    if not isinstance(pv, dict):
                        continue
                    for key in pv:
                        if any(kw in key.lower() for kw in ("shading", "solar", "blind")):
                            shading_elems.append(elem)
                            break
        except RuntimeError:
            pass

    if not shading_elems:
        return {}

    # Index shading elements by their spatial container or connected wall
    wall_index: Dict[int, list] = {}
    for se in shading_elems:
        # Try IfcRelFillsElement -> opening -> wall chain
        for rel in ifc_file.by_type("IfcRelFillsElement"):
            if rel.RelatedBuildingElement == se:
                opening = rel.RelatingOpeningElement
                for rel2 in ifc_file.by_type("IfcRelVoidsElement"):
                    if rel2.RelatedOpeningElement == opening:
                        wall = rel2.RelatingBuildingElement
                        wall_index.setdefault(wall.id(), []).append(se)

        # Try spatial containment: same storey
        for rel in getattr(se, "ContainedInStructure", []):
            container = rel.RelatingStructure
            wall_index.setdefault(container.id(), []).append(se)

        # Try direct connects
        for rel in getattr(se, "ConnectedTo", []):
            other = rel.RelatedElement
            if other.is_a("IfcWall") or other.is_a("IfcWallStandardCase"):
                wall_index.setdefault(other.id(), []).append(se)
        for rel in getattr(se, "ConnectedFrom", []):
            other = rel.RelatingElement
            if other.is_a("IfcWall") or other.is_a("IfcWallStandardCase"):
                wall_index.setdefault(other.id(), []).append(se)

    return wall_index


def _check_shading_index(window, shading_index: Dict[int, list], ifc_file) -> bool:
    """Check the pre-built shading index for the window's host wall or storey."""
    if not shading_index:
        return False

    # Check by host wall
    host = _find_host_wall(window, ifc_file)
    if host and host.id() in shading_index:
        return True

    # Check by spatial container (same storey)
    for rel in getattr(window, "ContainedInStructure", []):
        container = rel.RelatingStructure
        if container.id() in shading_index:
            return True

    return False


# ═══════════════════════════════════════════════════════════════════════════
# Tool schema & main check function
# ═══════════════════════════════════════════════════════════════════════════

TOOL_SCHEMA = {
    "name": "check_shading_presence",
    "description": (
        "Check whether windows on critical facades (e.g. South, West) have "
        "shading devices in an IFC building model"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "ifc_file_path": {
                "type": "string",
                "description": "Path to the IFC model file",
            },
            "critical_orientations": {
                "type": "string",
                "description": "Comma-separated orientations to check, e.g. 'S,SW,W' (default 'S,SW,W')",
            },
            "min_window_area": {
                "type": "number",
                "description": "Only flag windows larger than this area in m² (default 0.5)",
            },
            "true_north_deg": {
                "type": "number",
                "description": "Optional override for true north angle in degrees",
            },
        },
        "required": ["ifc_file_path"],
    },
}


def _window_label(window) -> str:
    """Return a human-readable label: Name + IFC id."""
    name = window.Name or ""
    return f"{name}:{window.id()}" if name else f"#{window.id()}"


def _is_critical(card: str, orientations: List[str]) -> bool:
    """True if *card* direction matches any of the *orientations*.

    Matching rules:
    - Exact match:  card='SW', orientations=['S','SW','W'] → True
    - Primary match: card='SW' matches if 'S' or 'W' is in orientations
    """
    if card in orientations:
        return True
    for ch in card:
        if ch in orientations:
            return True
    return False


def check_shading_presence(
    ifc_file_path: str,
    critical_orientations: str = "S,SW,W",
    min_window_area: float = 0.5,
    true_north_deg: Optional[float] = None,
) -> Dict[str, Any]:
    """Check whether windows on critical facades have shading devices."""
    try:
        ifc_file = ifcopenshell.open(ifc_file_path)
    except OSError:
        return {"error": f"Cannot open IFC file at '{ifc_file_path}'"}

    orientations = [o.strip().upper() for o in critical_orientations.split(",")]
    north_offset = (
        true_north_deg if true_north_deg is not None
        else _get_true_north(ifc_file)
    )

    # Build shading-element index once (for IFC2X3 compatibility)
    shading_index = _build_shading_index(ifc_file)

    results: List[Dict[str, Any]] = []
    unshaded_count = 0
    facade_summary: Dict[str, Dict[str, int]] = {}  # card -> {total, shaded}

    # Tracking skip reasons
    total_windows = 0
    skipped_small = 0
    skipped_internal = 0
    skipped_no_host = 0
    skipped_no_dir = 0
    skipped_non_critical = 0

    for window in ifc_file.by_type("IfcWindow"):
        total_windows += 1
        w_area = (window.OverallHeight or 0) * (window.OverallWidth or 0)
        if w_area < min_window_area:
            skipped_small += 1
            continue

        host_wall = _find_host_wall(window, ifc_file)
        if not host_wall:
            skipped_no_host += 1
            continue
        if not _is_external(host_wall):
            skipped_internal += 1
            continue

        d = _wall_direction(host_wall)
        if d is None:
            skipped_no_dir += 1
            continue

        angle = (_angle_from_vector(d[0], d[1]) + north_offset) % 360
        card = _cardinal(angle)

        if not _is_critical(card, orientations):
            skipped_non_critical += 1
            continue

        # Shading detection: helper + shading index fallback
        has_shading = _find_shading_for_window(window, ifc_file)
        if not has_shading:
            has_shading = _check_shading_index(window, shading_index, ifc_file)

        if not has_shading:
            unshaded_count += 1

        # Facade summary
        simple = _simple_cardinal(card)
        if simple not in facade_summary:
            facade_summary[simple] = {"total": 0, "shaded": 0, "unshaded": 0}
        facade_summary[simple]["total"] += 1
        if has_shading:
            facade_summary[simple]["shaded"] += 1
        else:
            facade_summary[simple]["unshaded"] += 1

        results.append({
            "window": _window_label(window),
            "area_m2": round(w_area, 3),
            "orientation": card,
            "has_shading": has_shading,
            "compliance": "PASS" if has_shading else "FAIL",
        })

    return {
        "results": results,
        "facade_summary": facade_summary,
        "total_windows": total_windows,
        "windows_checked": len(results),
        "shaded_count": len(results) - unshaded_count,
        "unshaded_count": unshaded_count,
        "skipped": {
            "too_small": skipped_small,
            "internal": skipped_internal,
            "no_host_wall": skipped_no_host,
            "no_direction": skipped_no_dir,
            "non_critical_facade": skipped_non_critical,
        },
        "overall_pass": unshaded_count == 0,
    }

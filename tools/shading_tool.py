# --- Check Tool 4: Shading Device Presence ---
from typing import List, Dict, Any, Optional
import math
import ifcopenshell
import ifcopenshell.util.element as util

def parse_orientations(orientations: str) -> set:
    """Parse comma-separated orientations to set of uppercase cardinal directions."""
    return set(o.strip().upper() for o in orientations.split(",") if o.strip())

def azimuth_to_cardinal8(azimuth_deg: float) -> str:
    """Convert azimuth in degrees to one of 8 cardinal directions (N, NE, E, SE, S, SW, W, NW)."""
    dirs = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
    idx = int(((azimuth_deg + 22.5) % 360) // 45)
    return dirs[idx]

def get_window_area_m2(window) -> Optional[float]:
    """Get window area in m² using OverallHeight × OverallWidth (consistent with other tools)."""
    # Primary: use OverallHeight × OverallWidth (nominal glazing area)
    h = getattr(window, 'OverallHeight', None)
    w = getattr(window, 'OverallWidth', None)
    if h and w and h > 0 and w > 0:
        return float(h) * float(w)
    
    # Fallback: try property sets for Height/Width
    try:
        psets = util.get_psets(window)
        for pvals in psets.values():
            if isinstance(pvals, dict):
                height = pvals.get('Height')
                width = pvals.get('Width')
                if height and width:
                    return float(height) * float(width)
    except Exception:
        pass
    return None

def _get_true_north(ifc_file) -> float:
    """Extract true north offset from IFC model."""
    try:
        for ctx in ifc_file.by_type('IfcGeometricRepresentationContext'):
            if ctx.is_a('IfcGeometricRepresentationSubContext'):
                continue
            if ctx.TrueNorth:
                dx, dy = ctx.TrueNorth.DirectionRatios[:2]
                return math.degrees(math.atan2(dx, dy))
    except Exception:
        pass
    return 0.0


def _build_window_to_wall_map(ifc_file) -> dict:
    """Build a dict {window: host_wall} via IfcRelFillsElement → IfcRelVoidsElement chain."""
    # Opening → Wall (from IfcRelVoidsElement)
    opening_to_wall = {}
    for void_rel in ifc_file.by_type('IfcRelVoidsElement'):
        opening_to_wall[void_rel.RelatedOpeningElement] = void_rel.RelatingBuildingElement
    
    # Window → Opening (from IfcRelFillsElement)
    window_to_wall = {}
    for fill_rel in ifc_file.by_type('IfcRelFillsElement'):
        opening = fill_rel.RelatingOpeningElement
        window = fill_rel.RelatedBuildingElement
        if opening in opening_to_wall:
            window_to_wall[window] = opening_to_wall[opening]
    return window_to_wall


def _wall_cardinal(wall, north_offset: float = 0.0) -> str:
    """Get cardinal direction (N/S/E/W) of a wall's outward-facing normal."""
    # Local axis from Axis representation
    local_dir = (1.0, 0.0)
    if wall.Representation:
        for rep in wall.Representation.Representations:
            if rep.RepresentationIdentifier == 'Axis':
                for item in rep.Items:
                    if item.is_a('IfcPolyline') and len(item.Points) >= 2:
                        pts = [p.Coordinates for p in item.Points]
                        dx, dy = pts[1][0] - pts[0][0], pts[1][1] - pts[0][1]
                        length = math.hypot(dx, dy)
                        if length > 0:
                            local_dir = (dx / length, dy / length)
    
    # Placement transform
    ref_dir = (1.0, 0.0)
    pl = wall.ObjectPlacement
    if pl and pl.is_a('IfcLocalPlacement'):
        rp = pl.RelativePlacement
        if rp and rp.RefDirection:
            rd = rp.RefDirection.DirectionRatios
            ref_dir = (rd[0], rd[1])
    
    perp = (-ref_dir[1], ref_dir[0])
    gdx = local_dir[0] * ref_dir[0] + local_dir[1] * perp[0]
    gdy = local_dir[0] * ref_dir[1] + local_dir[1] * perp[1]
    
    normal_angle = (math.degrees(math.atan2(gdy, gdx)) + 90 + north_offset) % 360
    
    if normal_angle >= 315 or normal_angle < 45:
        return 'E'
    elif normal_angle < 135:
        return 'N'
    elif normal_angle < 225:
        return 'W'
    else:
        return 'S'


def get_window_orientation(window, ifc_file, window_to_wall: dict, north_offset: float) -> str:
    """Get window orientation by finding its host wall and computing the wall's cardinal direction."""
    host_wall = window_to_wall.get(window)
    if host_wall:
        return _wall_cardinal(host_wall, north_offset)
    # Fallback: try window's own placement (less reliable)
    return 'N'

def detect_shading_for_window(window) -> bool:
    """Detect if window has shading device via relations or property sets."""
    # Check for related IfcShadingDevice
    try:
        # IfcRelConnectsElements
        if hasattr(window, 'ConnectedTo'):
            for rel in window.ConnectedTo:
                if hasattr(rel, 'RelatedElement') and rel.RelatedElement and rel.RelatedElement.is_a('IfcShadingDevice'):
                    return True
        # IfcRelAggregates
        if hasattr(window, 'IsDecomposedBy'):
            for rel in window.IsDecomposedBy:
                for obj in getattr(rel, 'RelatedObjects', []):
                    if obj.is_a('IfcShadingDevice'):
                        return True
        # IfcRelContainedInSpatialStructure
        if hasattr(window, 'ContainedInStructure'):
            for rel in window.ContainedInStructure:
                for obj in getattr(rel, 'RelatedElements', []):
                    if obj.is_a('IfcShadingDevice'):
                        return True
    except Exception:
        pass
    # Check property sets for shading keys
    try:
        keys = ["Shading", "HasShading", "ExternalShading", "ProtectionSolar"]
        if hasattr(window, 'IsDefinedBy'):
            for rel in window.IsDefinedBy:
                if rel.is_a('IfcRelDefinesByProperties'):
                    prop_set = rel.RelatingPropertyDefinition
                    if hasattr(prop_set, 'HasProperties'):
                        for prop in prop_set.HasProperties:
                            if any(k.lower() in (prop.Name or '').lower() for k in keys):
                                val = getattr(prop, 'NominalValue', None)
                                if val is not None and str(val).lower() in ("true", "yes", "1"):
                                    return True
    except Exception:
        pass
    return False

def check_shading_presence(
    ifc_file_path: str,
    critical_orientations: str = "S,SW,W",
    min_window_area: float = 1.0,
    true_north_deg: Optional[float] = None
) -> dict:
    """
    Check for shading device presence on windows in critical orientations and area.
    Returns dict with keys: results, unshaded_count, compliant
    """
    ifc = ifcopenshell.open(ifc_file_path)
    crit_set = parse_orientations(critical_orientations)
    
    # Pre-compute orientation helpers (consistent with wwr_tool / shgc_tool)
    north_offset = true_north_deg if true_north_deg is not None else _get_true_north(ifc)
    window_to_wall = _build_window_to_wall_map(ifc)
    
    results = []
    unshaded_count = 0
    for window in ifc.by_type('IfcWindow'):
        name = getattr(window, 'Name', None) or str(window.id())
        global_id = window.GlobalId
        area_m2 = get_window_area_m2(window)
        orientation = get_window_orientation(window, ifc, window_to_wall, north_offset)
        has_shading = detect_shading_for_window(window)
        # Compliance logic
        is_critical = orientation in crit_set and (area_m2 is None or area_m2 > min_window_area)
        if is_critical:
            if has_shading:
                check_status = "pass"
            else:
                check_status = "fail"
                unshaded_count += 1
        else:
            check_status = "log"   # non-critical facade → informational

        actual = "has shading" if has_shading else "no shading"
        required = f"shading required ({critical_orientations} facade, > {min_window_area} m2)" if is_critical else None
        area_str = f"{round(area_m2, 2)}" if area_m2 is not None else "unknown"

        results.append({
            "element_id":        global_id,
            "element_type":      "IfcWindow",
            "element_name":      name,
            "element_name_long": None,
            "check_status":      check_status,
            "actual_value":      actual,
            "required_value":    required,
            "comment":           f"orientation={orientation}, area_m2={area_str}",
            "log":               None if is_critical else f"SKIP: non-critical facade ({orientation})",
        })

    # Build overall_result
    total = len(results)
    n_checked = sum(1 for r in results if r["check_status"] != "log")
    if unshaded_count == 0:
        summary = (
            f"All {n_checked} window(s) on critical facades ({critical_orientations}) "
            f"have shading. {total} windows total."
        )
    else:
        summary = (
            f"{unshaded_count} window(s) on critical facades ({critical_orientations}) "
            f"lack shading devices. {n_checked} checked, {total} windows total."
        )

    overall_result = {
        "status":       "pass" if unshaded_count == 0 else "fail",
        "summary":      summary,
        "has_elements":  1 if results else 0,
    }

    return results, overall_result

# --- Check Tool 4: Shading Device Presence ---
from typing import List, Dict, Any, Optional
import math

def parse_orientations(orientations: str) -> set:
    """Parse comma-separated orientations to set of uppercase cardinal directions."""
    return set(o.strip().upper() for o in orientations.split(",") if o.strip())

def azimuth_to_cardinal8(azimuth_deg: float) -> str:
    """Convert azimuth in degrees to one of 8 cardinal directions (N, NE, E, SE, S, SW, W, NW)."""
    dirs = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
    idx = int(((azimuth_deg + 22.5) % 360) // 45)
    return dirs[idx]

def get_window_area_m2(window) -> Optional[float]:
    """Try to get window area in m² from quantities or geometry. Return None if not possible."""
    # Try IfcQuantityArea from quantities
    if hasattr(window, 'IsDefinedBy'):
        for rel in window.IsDefinedBy:
            if rel.is_a('IfcRelDefinesByProperties'):
                prop_set = rel.RelatingPropertyDefinition
                if prop_set.is_a('IfcElementQuantity'):
                    for q in prop_set.Quantities:
                        if q.is_a('IfcQuantityArea') and 'Area' in (q.Name or ''):
                            try:
                                return float(q.AreaValue)
                            except Exception:
                                pass
    # Try geometry (ifcopenshell.geom)
    try:
        import ifcopenshell.geom
        settings = ifcopenshell.geom.settings()
        settings.set(settings.USE_WORLD_COORDS, True)
        shape = ifcopenshell.geom.create_shape(settings, window)
        verts = shape.geometry.verts
        faces = shape.geometry.faces
        # Approximate area by summing triangle areas
        def tri_area(i1, i2, i3):
            p1 = verts[i1*3:i1*3+3]
            p2 = verts[i2*3:i2*3+3]
            p3 = verts[i3*3:i3*3+3]
            a = [p2[i] - p1[i] for i in range(3)]
            b = [p3[i] - p1[i] for i in range(3)]
            cross = [a[1]*b[2] - a[2]*b[1], a[2]*b[0] - a[0]*b[2], a[0]*b[1] - a[1]*b[0]]
            return 0.5 * math.sqrt(sum(c**2 for c in cross))
        area = 0.0
        for i in range(0, len(faces), 3):
            area += tri_area(faces[i], faces[i+1], faces[i+2])
        return area
    except Exception:
        pass
    return None

def get_window_azimuth_deg(window, ifc_file, true_north_deg: Optional[float]=None) -> float:
    """Estimate window azimuth (0=N, 90=E, 180=S, 270=W) in degrees, considering true north."""
    # Try to get window local placement direction
    try:
        placement = window.ObjectPlacement
        while placement and not placement.is_a('IfcLocalPlacement'):
            placement = getattr(placement, 'PlacementRelTo', None)
        if placement:
            axis = placement.RelativePlacement
            if hasattr(axis, 'RefDirection') and axis.RefDirection:
                dir_rat = axis.RefDirection.DirectionRatios
                x, y = dir_rat[0], dir_rat[1] if len(dir_rat) > 1 else 0.0
                az = math.degrees(math.atan2(x, y)) % 360
            else:
                az = 0.0
        else:
            az = 0.0
    except Exception:
        az = 0.0
    # Adjust for true north
    if true_north_deg is not None:
        az = (az + true_north_deg) % 360
    else:
        # Try to get project north from IfcProject/IfcSite
        try:
            project = ifc_file.by_type('IfcProject')[0]
            if hasattr(project, 'RepresentationContexts'):
                for ctx in project.RepresentationContexts:
                    if hasattr(ctx, 'TrueNorth') and ctx.TrueNorth:
                        dir_rat = ctx.TrueNorth.DirectionRatios
                        x, y = dir_rat[0], dir_rat[1] if len(dir_rat) > 1 else 0.0
                        north_az = math.degrees(math.atan2(x, y)) % 360
                        az = (az + north_az) % 360
                        break
        except Exception:
            pass
    return az

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
    Returns dict with keys: results, unshaded_count, overall_pass
    """
    ifc = ifcopenshell.open(ifc_file_path)
    crit_set = parse_orientations(critical_orientations)
    results = []
    unshaded_count = 0
    for window in ifc.by_type('IfcWindow'):
        name = getattr(window, 'Name', None) or getattr(window, 'GlobalId', None) or str(window.id())
        area_m2 = get_window_area_m2(window)
        azimuth = get_window_azimuth_deg(window, ifc, true_north_deg)
        orientation = azimuth_to_cardinal8(azimuth)
        has_shading = detect_shading_for_window(window)
        # Compliance logic
        compliance = "SKIP"
        note = ""
        if orientation in crit_set and (area_m2 is None or area_m2 > min_window_area):
            if has_shading:
                compliance = "PASS"
            else:
                compliance = "FAIL"
                unshaded_count += 1
        else:
            compliance = "SKIP"
            if area_m2 is None:
                note = "Area unknown, not filtered by area."
        results.append({
            "name": name,
            "area_m2": area_m2,
            "orientation": orientation,
            "has_shading": has_shading,
            "compliance": compliance,
            **({"note": note} if note else {})
        })
    overall_pass = (unshaded_count == 0)
    return {
        "results": results,
        "unshaded_count": unshaded_count,
        "overall_pass": overall_pass
    }
"""IFC Compliance Checker — YOUR CODE GOES HERE"""

import ifcopenshell


# ─── Write your check functions below ──────────────────────────
# Each function takes a model, returns a list of strings.
# One string per element you checked.

def check_door_width(model, min_width_mm=800):
    """Check that all doors are at least 800mm wide."""
    results = []
    for door in model.by_type("IfcDoor"):
        width_m = door.OverallWidth  # IFC stores in meters
        width_mm = round(width_m * 1000) if width_m else None
        if width_mm is None:
            results.append(f"[???] {door.Name}: width unknown")
        elif width_mm >= min_width_mm:
            results.append(f"[PASS] {door.Name}: {width_mm} mm (min {min_width_mm} mm)")
        else:
            results.append(f"[FAIL] {door.Name}: {width_mm} mm (min {min_width_mm} mm)")
    return results


# def check_room_area(model):
#     """Your next check..."""
#     results = []
#     for space in model.by_type("IfcSpace"):
#         ...
#     return results

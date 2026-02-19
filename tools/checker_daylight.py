import ifcopenshell
import ifcopenshell.util.element as util
import ifcopenshell.util.placement as placement
import math

def _extract_quantity_value(q):
    """Extract the numeric value from any IfcPhysicalQuantity subtype."""
    for attr in ("LengthValue", "AreaValue", "VolumeValue",
                 "CountValue", "WeightValue", "TimeValue",
                 "NominalValue"):
        v = getattr(q, attr, None)
        if v is not None:
            return v
    return None


def get_quantity(element, quantity_name):
    """
    Tries to retrieve a quantity from various property sets.
    Works across IFC2x3, IFC4, and IFC4x3.
    """
    val = 0.0
    # 1. Try IfcElementQuantity (common in IFC4)
    for rel in getattr(element, "IsDefinedBy", []):
        if rel.is_a("IfcRelDefinesByProperties"):
            props = rel.RelatingPropertyDefinition
            if props.is_a("IfcElementQuantity"):
                for q in props.Quantities:
                    if q.Name == quantity_name:
                        v = _extract_quantity_value(q)
                        if v is not None:
                            val = v
                        break

    # 2. Try standard property sets via util.get_psets (handles BaseQuantities, etc.)
    if val == 0.0:
        psets = util.get_psets(element)
        for pset_name, properties in psets.items():
            if quantity_name in properties:
                 val = properties[quantity_name]
                 break

    # Attempt to convert to float
    try:
        return float(val)
    except (ValueError, TypeError):
        return 0.0

def check_daylight_glazing_ratio(model, min_ratio=0.10, room_types=None):
    # Normalize room_types to a list of lower-case strings for matching
    allowed_types = []
    if room_types:
        if isinstance(room_types, str):
            allowed_types = [t.strip().lower() for t in room_types.split(",") if t.strip()]
        elif isinstance(room_types, list):
            allowed_types = [t.strip().lower() for t in room_types if isinstance(t, str) and t.strip()]

    results = []
    
    # Iterate over all spaces/rooms
    for space in model.by_type("IfcSpace"):
        
        # --- Type Extraction ---
        type_name = space.ObjectType or ""
        if not type_name:
             for rel in getattr(space, "IsTypedBy", []):
                 if rel.RelatingType and rel.RelatingType.Name:
                     type_name = rel.RelatingType.Name
                     break
        if not type_name:
            type_name = space.LongName or ""
            
        space_name_full = (space.Name or "") + " " + (type_name or "")
        space_name_lower = space_name_full.lower()

        # --- Determing Requirements based on Room Type (Catalan Regulations) ---
        req_ratio = min_ratio
        is_habitable = False
        
        # Check if habitable (Bedroom, Living, Kitchen, Dining)
        habitable_keywords = ["bedroom", "living", "kitchen", "dining", "dormitorio", "estar", "cocina", "comedor", "sala"]
        if any(k in space_name_lower for k in habitable_keywords):
            req_ratio = 0.125
            is_habitable = True
        
        # --- Filter by Room Type ---
        if allowed_types:
            # Check Name, LongName, and potentially ObjectType
            space_name = (space.Name or "").lower()
            space_longname = (space.LongName or "").lower()
            space_obj_type = (space.ObjectType or "").lower()
            
            # If none of the allowed types are found in the space identifiers, skip
            if not any(t in space_name or t in space_longname or t in space_obj_type for t in allowed_types):
                continue

        # --- Floor Area Extraction ---
        floor_area = get_quantity(space, "NetFloorArea")
        if floor_area <= 0:
             floor_area = get_quantity(space, "GrossFloorArea") # Fallback
        if floor_area <= 0:
             floor_area = get_quantity(space, "Area") # Another Fallback

        if floor_area <= 0:
            # If we still don't have area, we can't calculate a ratio
            pass 

        # --- Window Area Extraction ---
        win_area = 0.0
        
        # Get Space Elevation (Global Z)
        space_z = 0.0
        try:
            space_matrix = placement.get_local_placement(space.ObjectPlacement)
            space_z = space_matrix[2][3] # Z translation
        except:
            pass

        # Find boundaries
        for rel in getattr(space, "BoundedBy", []):
            if rel.is_a("IfcRelSpaceBoundary") and rel.RelatedBuildingElement:
                el = rel.RelatedBuildingElement
                
                # Check if it is a window or a door acting as window? (Usually just IfcWindow)
                if el.is_a("IfcWindow") or el.is_a("IfcDoor"): 
                    # Note: Some full-glass doors might count, but usually we stick to Windows.
                    # Strict logic: only IfcWindow.
                    if not el.is_a("IfcWindow"):
                        continue
                        
                    # Calculate Area for this Window
                    w_area = get_quantity(el, "Area")
                    
                    # Dimensions
                    h = getattr(el, "OverallHeight", 0) or get_quantity(el, "Height")
                    w = getattr(el, "OverallWidth", 0) or get_quantity(el, "Width")
                    
                    if w_area <= 0 and h and w:
                        w_area = h * w
                    
                    # --- Height Constraint Check (0 to 2.5m) ---
                    # Only if we have geometry info
                    effective_area = w_area
                    
                    if h > 0:
                        try:
                            # Get Window Elevation (Global Z)
                            win_matrix = placement.get_local_placement(el.ObjectPlacement)
                            win_z = win_matrix[2][3]
                            
                            rel_z = win_z - space_z
                            
                            # Valid range relative to floor: [0, 2.5]
                            # Window range relative to floor: [rel_z, rel_z + h]
                            
                            valid_bottom = max(0, rel_z)
                            valid_top = min(2.5, rel_z + h)
                            
                            if valid_top > valid_bottom:
                                valid_h = valid_top - valid_bottom
                                fraction = valid_h / h
                                effective_area = w_area * fraction
                            else:
                                effective_area = 0.0 # Entirely outside range
                        except:
                            # If placement fails, assume 100% effective (fallback)
                            pass
                    
                    win_area += effective_area

        # Calculate Ratio
        ratio = 0.0
        if floor_area > 0:
            ratio = win_area / floor_area

        if win_area <= 0:
            check_status = "blocked"
        elif ratio < req_ratio:
            check_status = "fail"
        else:
            check_status = "pass"

        results.append({
            "element_id":        space.GlobalId,
            "element_type":      "IfcSpace",
            "element_name":      space.Name or "Unnamed",
            "element_name_long": type_name or None,
            "check_status":      check_status,
            "actual_value":      f"{round(ratio * 100, 1)}%" if floor_area > 0 else None,
            "required_value":    f">= {round(req_ratio * 100, 1)}%",
            "comment":           f"floor_area_m2={round(floor_area, 2)}, window_area_m2={round(win_area, 2)}",
            "log":               "No windows found" if win_area <= 0 else None,
        })

    # Summary
    n_fail = sum(1 for r in results if r["check_status"] == "fail")
    n_blocked = sum(1 for r in results if r["check_status"] == "blocked")
    all_pass = (n_fail == 0 and len(results) > 0)

    if all_pass and n_blocked == 0:
        summary = f"All {len(results)} rooms meet daylight glazing ratio requirements."
    elif all_pass:
        summary = f"All rooms with windows pass. {n_blocked} room(s) have no windows (blocked)."
    else:
        summary = f"{n_fail} room(s) fail daylight glazing ratio. {n_blocked} room(s) have no windows."

    return results
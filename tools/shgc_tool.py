
import ifcopenshell
import ifcopenshell.util.element as util
import ifcopenshell.util.placement as placement
import math

def get_quantity(element, quantity_name):
    """
    Tries to retrieve a quantity from various property sets.
    """
    val = 0.0
    # 1. Try IfcElementQuantity (common in IFC4)
    for rel in getattr(element, "IsDefinedBy", []):
        if rel.is_a("IfcRelDefinesByProperties"):
            props = rel.RelatingPropertyDefinition
            if props.is_a("IfcElementQuantity"):
                for q in props.Quantities:
                    if q.Name == quantity_name:
                        val = q.NominalValue
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

def get_window_orientation(window, ifc_file):
    """
    Calculates the cardinal orientation (N, S, E, W) of a window.
    Returns: 'N', 'S', 'E', 'W' or 'Unknown'
    """
    try:
        # Get rotation matrix of the window in world coordinates
        matrix = placement.get_local_placement(window.ObjectPlacement)
        
        # Determine orientation from the matrix rotation
        # In IFC, local Y (index 1) typically points outward/normal to the wall for a window
        facing_x = matrix[0][1]
        facing_y = matrix[1][1]
        
        # Determine dominant direction
        abs_x = abs(facing_x)
        abs_y = abs(facing_y)
        
        if abs_x > abs_y:
            # East or West
            return "E" if facing_x > 0 else "W"
        else:
            # North or South
            return "N" if facing_y > 0 else "S"
            
    except Exception as e:
        print(f"Error calculating orientation for {window.GlobalId}: {e}")
        return "Unknown"

def get_shgc_value(window):
    """
    Retrieves SHGC value from property sets.
    """
    psets = util.get_psets(window)
    candidates = ["Pset_DoorWindowGlazingType", "Pset_WindowCommon", "Energy", "Thermal", "Pset_GlazingTypeCommon"]
    props = ["SolarHeatGainCoefficient", "SHGC", "TotalSolarHeatTransmittance", "SolarFactor", "g"]
    
    for pset_name in psets:
        for prop_name in psets[pset_name]:
            if prop_name in props:
                return psets[pset_name][prop_name]
    return None

def check_shgc(ifc_file_path, default_shgc=0.70, **kwargs):
    try:
        model = ifcopenshell.open(ifc_file_path)
    except Exception as e:
        return {"error": f"Could not open file: {e}"}

    # CTE DB-HE 2019/2022 Parameters for Climate Zone C2 (Barcelona)
    h_sol_jul_c2 = {
        "N": 45.0,  # North
        "S": 66.0,  # South
        "E": 112.0, # East
        "W": 112.0, # West
        "Unknown": 100.0
    }
    
    # Limit for q_sol;jul
    q_sol_jul_limit = 2.0

    # 1. Calculate A_util (Total Useful Floor Area)
    a_util = 0.0
    spaces = model.by_type("IfcSpace")
    for space in spaces:
        area = get_quantity(space, "NetFloorArea")
        if area <= 0: area = get_quantity(space, "GrossFloorArea")
        if area <= 0: area = get_quantity(space, "Area")
        a_util += area
        
    if a_util <= 0:
        return {"error": "Could not determine useful floor area (A_util) to calculate q_sol;jul."}

    results = []
    total_heat_gain = 0.0
    missing_shgc_count = 0
    total_window_area = 0.0
    
    windows = model.by_type("IfcWindow")
    if not windows:
         return {"error": "No windows found in the model"}

    for window in windows:
        # Geometry
        w_area = get_quantity(window, "Area")
        if w_area <= 0:
             h = getattr(window, "OverallHeight", 0) or get_quantity(window, "Height")
             w = getattr(window, "OverallWidth", 0) or get_quantity(window, "Width")
             if h and w: w_area = h * w
        
        total_window_area += w_area
        
        # Orientation
        orientation = get_window_orientation(window, model)
        
        # SHGC
        shgc = get_shgc_value(window)
        is_missing = False
        if shgc is None:
            shgc = default_shgc
            is_missing = True
            missing_shgc_count += 1
        else:
            try:
                shgc = float(shgc)
            except:
                shgc = default_shgc
                is_missing = True
                missing_shgc_count += 1
        
        # Parameters for Calculation
        # F_sh_obst: 1.0, FF: 0.25
        f_sh_obst = 1.0 
        ff = 0.25
        g_val = shgc
        
        h_sol = h_sol_jul_c2.get(orientation, 100.0)
        
        # Contribution
        contribution = f_sh_obst * g_val * (1 - ff) * w_area * h_sol
        
        total_heat_gain += contribution
            
        # Per-window status: "warning" if SHGC missing, else "log" (pass/fail is building-level)
        if is_missing:
            row_status = "warning"
        else:
            row_status = "log"

        results.append({
            "element_id":        window.GlobalId,
            "element_type":      "IfcWindow",
            "element_name":      window.Name or "Unnamed",
            "element_name_long": None,
            "check_status":      row_status,
            "actual_value":      f"SHGC={round(shgc, 3)}, contribution={round(contribution, 2)} Wh",
            "required_value":    f"q_sol_jul <= {q_sol_jul_limit} kWh/m2·month (building level)",
            "comment":           f"orientation={orientation}, area_m2={round(w_area, 2)}",
            "log":               "SHGC not in IFC (default 0.7)" if is_missing else None,
        })
        
    # Calculate q_sol;jul
    q_sol_jul = total_heat_gain / a_util
    passed = bool(q_sol_jul <= q_sol_jul_limit)
    
    if passed:
        summary = (
            f"Building passes solar heat gain check. "
            f"q_sol_jul={round(q_sol_jul, 2)} <= {q_sol_jul_limit} kWh/m2·month. "
            f"A_util={round(a_util, 2)} m2, {len(windows)} windows checked."
        )
    else:
        summary = (
            f"Building FAILS solar heat gain check. "
            f"q_sol_jul={round(q_sol_jul, 2)} > {q_sol_jul_limit} kWh/m2·month. "
            f"A_util={round(a_util, 2)} m2, {len(windows)} windows checked."
        )
    if missing_shgc_count:
        summary += f" {missing_shgc_count} window(s) missing SHGC (default 0.7 used)."

    overall_result = {
        "status":       "pass" if passed else "fail",
        "summary":      summary,
        "has_elements":  1 if results else 0,
    }

    return results, overall_result

def analyze_shgc(ifc_file_path):
    """
    Standard entry point for the SHGC Compliance Tool.
    Hardcodes Climate Zone C2 values and returns JSON.
    """
    return check_shgc(ifc_file_path)

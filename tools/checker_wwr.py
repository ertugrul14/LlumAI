"""
Tool 2 – Window-to-Wall Ratio (WWR) by Facade
==============================================
Standalone tool function for the ifcore-platform orchestrator.

Regulation : CTE DB-HE 1 (Spanish Technical Building Code – Energy Efficiency)
Thresholds : Conservative geometric pre-screening values for Zone C2
             (Barcelona / coastal Catalonia). Full regulatory compliance also
             requires q_sol;jul ≤ 2.00 kWh/m²·month (Table 3.1.2-HE1).

Hardcoded thresholds (Zone C2, CTE DB-HE 1):
    North  → 35 %
    South  → 30 %   ← stricter: highest summer solar risk
    East   → 25 %
    West   → 22 %   ← strictest: afternoon overheating risk
"""

import math
import json
import uuid
import time
import ifcopenshell
import ifcopenshell.util.element as util

CARDINAL_LONG = {"N": "North", "S": "South", "E": "East", "W": "West"}

# ---------------------------------------------------------------------------
# Hardcoded thresholds – Zone C2 (Barcelona / coastal Catalonia)
# Change these if targeting a different CTE climate zone.
# ---------------------------------------------------------------------------
MAX_WWR = {
    "N": 0.35,
    "S": 0.30,
    "E": 0.25,
    "W": 0.22,
}

REGULATION = "CTE DB-HE 1 – geometric pre-screening (Zone C2)"


# ---------------------------------------------------------------------------
# Private helpers (geometry + IFC traversal)
# ---------------------------------------------------------------------------

def _dms_to_decimal(dms):
    if not dms:
        return None
    sign = -1 if dms[0] < 0 else 1
    d, m, s, ms = (list(dms) + [0, 0, 0, 0])[:4]
    return sign * (abs(d) + abs(m) / 60 + abs(s) / 3600 + abs(ms) / 3_600_000_000)


def _true_north(ifc_file):
    for ctx in ifc_file.by_type("IfcGeometricRepresentationContext"):
        if ctx.is_a("IfcGeometricRepresentationSubContext"):
            continue
        if ctx.TrueNorth:
            dx, dy = ctx.TrueNorth.DirectionRatios[:2]
            return math.degrees(math.atan2(dx, dy))
    return 0.0


def _is_external(wall):
    psets = util.get_psets(wall)
    return psets.get("Pset_WallCommon", {}).get("IsExternal", False)


def _is_foundation(wall):
    return "foundation" in (wall.Name or "").lower()


def _wall_area(wall):
    psets = util.get_psets(wall)
    area = psets.get("PSet_Revit_Dimensions", {}).get("Area")
    if area and area > 0:
        return float(area)
    for rel in getattr(wall, "IsDefinedBy", []):
        if rel.is_a("IfcRelDefinesByProperties"):
            pd = rel.RelatingPropertyDefinition
            if pd.is_a("IfcElementQuantity"):
                for q in pd.Quantities:
                    if q.is_a("IfcQuantityArea") and "gross" in (q.Name or "").lower():
                        return float(q.AreaValue)
    return 0.0


def _cardinal(wall, north_offset=0.0):
    # Local axis from Axis representation
    local_dir = (1.0, 0.0)
    if wall.Representation:
        for rep in wall.Representation.Representations:
            if rep.RepresentationIdentifier == "Axis":
                for item in rep.Items:
                    if item.is_a("IfcPolyline") and len(item.Points) >= 2:
                        pts = [p.Coordinates for p in item.Points]
                        dx, dy = pts[1][0] - pts[0][0], pts[1][1] - pts[0][1]
                        length = math.hypot(dx, dy)
                        if length > 0:
                            local_dir = (dx / length, dy / length)

    # Placement transform
    ref_dir = (1.0, 0.0)
    pl = wall.ObjectPlacement
    if pl and pl.is_a("IfcLocalPlacement"):
        rp = pl.RelativePlacement
        if rp and rp.RefDirection:
            rd = rp.RefDirection.DirectionRatios
            ref_dir = (rd[0], rd[1])

    perp = (-ref_dir[1], ref_dir[0])
    gdx = local_dir[0] * ref_dir[0] + local_dir[1] * perp[0]
    gdy = local_dir[0] * ref_dir[1] + local_dir[1] * perp[1]

    normal_angle = (math.degrees(math.atan2(gdy, gdx)) + 90 + north_offset) % 360

    if normal_angle >= 315 or normal_angle < 45:
        return "E"
    elif normal_angle < 135:
        return "N"
    elif normal_angle < 225:
        return "W"
    else:
        return "S"


def _window_area(window):
    h, w = window.OverallHeight, window.OverallWidth
    if h and w and h > 0 and w > 0:
        return float(h) * float(w)
    psets = util.get_psets(window)
    for pvals in psets.values():
        if isinstance(pvals, dict):
            height = pvals.get("Height")
            width  = pvals.get("Width")
            if height and width:
                return float(height) * float(width)
    return 0.0


def _window_wall_map(ifc_file):
    opening_to_wall = {
        rel.RelatedOpeningElement: rel.RelatingBuildingElement
        for rel in ifc_file.by_type("IfcRelVoidsElement")
    }
    return {
        rel.RelatedBuildingElement: opening_to_wall[rel.RelatingOpeningElement]
        for rel in ifc_file.by_type("IfcRelFillsElement")
        if rel.RelatingOpeningElement in opening_to_wall
    }


# ---------------------------------------------------------------------------
# Public tool function
# ---------------------------------------------------------------------------

def check_wwr(model):
    """
    Compute Window-to-Wall Ratio per facade orientation and check compliance.

    Args:
        model: An ifcopenshell.file object (already opened).

    Returns:
        list[dict] — one dict per element (one per facade N/S/E/W).
    """
    ifc_file = model

    # -- Extract project_id --------------------------------------------------
    projects = ifc_file.by_type("IfcProject")
    project_id = projects[0].GlobalId if projects else None

    north_offset = _true_north(ifc_file)

    # -- Site info -----------------------------------------------------------
    site_info = {"latitude": None, "longitude": None, "true_north_deg": north_offset}
    sites = ifc_file.by_type("IfcSite")
    if sites:
        s = sites[0]
        site_info["latitude"]  = _dms_to_decimal(s.RefLatitude)
        site_info["longitude"] = _dms_to_decimal(s.RefLongitude)

    # -- Accumulate wall areas per cardinal ----------------------------------
    wall_areas   = {"N": 0.0, "S": 0.0, "E": 0.0, "W": 0.0}
    wall_orients = {}  # wall.id() -> cardinal

    for wtype in ("IfcWallStandardCase", "IfcWall"):
        for wall in ifc_file.by_type(wtype):
            if not _is_external(wall) or _is_foundation(wall):
                continue
            card = _cardinal(wall, north_offset)
            area = _wall_area(wall)
            wall_areas[card]      += area
            wall_orients[wall.id()] = card

    # -- Accumulate window areas per cardinal --------------------------------
    win_areas = {"N": 0.0, "S": 0.0, "E": 0.0, "W": 0.0}
    win_wall_map = _window_wall_map(ifc_file)
    warnings = []
    unmatched = 0

    for window in ifc_file.by_type("IfcWindow"):
        host = win_wall_map.get(window)
        if host is None:
            unmatched += 1
            continue
        card = wall_orients.get(host.id()) or _cardinal(host, north_offset)
        win_areas[card] += _window_area(window)

    if unmatched:
        warnings.append(f"{unmatched} window(s) could not be matched to a host wall.")

    # -- Compute WWR and compliance ------------------------------------------
    element_results = []
    all_pass = True
    fail_cards = []

    for card in ("N", "S", "E", "W"):
        w_area  = wall_areas[card]
        g_area  = win_areas[card]
        wwr     = (g_area / w_area) if w_area > 0 else 0.0
        limit   = MAX_WWR[card]
        passed  = wwr <= limit

        if not passed:
            all_pass = False
            fail_cards.append(card)
            warnings.append(
                f"{card} facade exceeds limit: {wwr*100:.1f}% > {limit*100:.0f}%"
            )

        element_results.append({
            "element_id":        None,
            "element_type":      "Facade",
            "element_name":      card,
            "element_name_long": CARDINAL_LONG[card],
            "check_status":      "pass" if passed else "fail",
            "actual_value":      f"{round(wwr * 100, 1)}%",
            "required_value":    f"<= {round(limit * 100, 1)}%",
            "comment":           f"wall_area_m2={round(w_area, 2)}, window_area_m2={round(g_area, 2)}",
            "log":               None,
        })

    return element_results

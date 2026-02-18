# IFC Solar & Daylight Compliance Checker

An AI-powered system that automatically verifies building information models (BIM) against solar radiation and daylight regulations using IFC data extraction and Google Gemini as the LLM orchestrator.

---

## Project Overview

This is a **collaborative 6-team effort**. Each team develops one compliance-check tool that extracts specific geometric or property data from IFC files and evaluates it against international building codes. A shared orchestrator registers all tools as Gemini function calls, allowing users to query building models in natural language and receive compliance verdicts.

### The 6 Tools

| # | Tool | Checks | Regulation |
|---|------|--------|------------|
| 1 | Minimum Daylight Glazing Ratio | Window area ≥ 10% of floor area per room | IBC §1205 / RT2012 |
| 2 | Window-to-Wall Ratio (WWR) by Facade | Glazing % per facade orientation | CTE DB-HE 1 |
| 3 | Solar Heat Gain Coefficient (SHGC) | SHGC per window vs. orientation limits | IECC C402.4 / ASHRAE 90.1 |
| 4 | Shading Device Presence | S/W windows have shading elements | EN 14501 / RE2020 |
| 5 | Room Depth vs. Daylight Penetration | Depth ≤ 2.5 × window head height | BS 8206-2 / EN 17037 |
| 6 | Skylight Solar Exposure | Skylight ratio 3–5%, SHGC limits | ASHRAE 90.1 / IECC C402 |

---

## Architecture

**Data Flow:**
1. User provides an IFC file and asks a compliance question in natural language
2. Google Gemini interprets the query and selects relevant tools
3. Each tool function extracts data from the IFC model and returns a JSON compliance result
4. Gemini combines results and generates a natural-language summary

**Tech Stack:**
- [`ifcopenshell`](https://ifcopenshell.org/) — IFC parsing and geometry extraction
- [`google-generativeai`](https://ai.google.dev/) — Google Gemini SDK (function calling)
- [Python 3.10+](https://www.python.org/)
- [`python-dotenv`](https://github.com/theskumar/python-dotenv) — API key management

---

## Repository Structure

Each team contributes a Python tool file to `teams/<team-name>/tools/` with this signature:

```python
def check_<tool_name>(ifc_file_path: str) -> dict:
    """Analyse IFC model and return compliance result as JSON."""
    return {
        "compliant": bool,
        "regulation": str,
        # ... tool-specific data fields
    }
```

The shared orchestrator (`orchestrator.py`) imports all tools and registers them with Gemini for function calling.

---

## Shared Objectives

- ✅ Automated BIM compliance checking against multiple international codes
- ✅ Performance-based, regulation-aware analysis (not prescriptive-only)
- ✅ Modular design: each tool independently testable
- ✅ Natural-language interface via Gemini orchestration
- ✅ JSON API: suitable for CI/CD pipelines and downstream integrations

---

## References

- [Gemini Function Calling Guide](https://ai.google.dev/docs/function_calling)
- [IfcOpenShell Documentation](https://docs.ifcopenshell.org/)
- CTE DB-HE 1 (Spanish Technical Building Code)
- ASHRAE 90.1, IECC, EN 15804


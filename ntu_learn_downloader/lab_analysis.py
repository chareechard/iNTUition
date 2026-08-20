"""Validate FRIDAY's read of a Lab file into a safe-to-render algorithm blueprint.

The model's JSON describes arbitrary student code and is untrusted output,
not a value to hand straight to the page.
Every field is shape- and range-checked before it reaches the dashboard;
anything that doesn't fit is dropped rather than passed through. The
"steps" list is this module's own extension to the brief's JSON contract -
"initialState" alone can seed a static picture but not a Play/Step/Speed
timeline, so the model is additionally asked to report the operation trace
(compare/swap/set/visit/edge) the Simulation tab plays back.
"""
import json
import re
from typing import Dict, List, Optional

MAX_CRITICAL_LINES = 8
MAX_STEPS = 300
MAX_TEXT_LEN = 160
SIMULATION_TYPES = ("array", "graph", "tree", "none")
STEP_OPS = ("compare", "swap", "set", "visit", "edge", "relax")


def _extract_json(raw: str) -> Optional[Dict]:
    text = (raw or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except ValueError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            data = json.loads(text[start:end + 1])
            return data if isinstance(data, dict) else None
        except ValueError:
            return None
    return None


def _text(value: object, default: str = "") -> str:
    s = re.sub(r"\s+", " ", str(value or "")).strip()
    return s[:MAX_TEXT_LEN] if s else default


def _line_number(value: object, line_count: int) -> Optional[int]:
    try:
        line = int(value)
    except (TypeError, ValueError):
        return None
    if line < 1 or (line_count and line > line_count):
        return None
    return line


def _critical_lines(value: object, line_count: int) -> List[Dict]:
    if not isinstance(value, list):
        return []
    out = []
    for item in value[:MAX_CRITICAL_LINES]:
        if not isinstance(item, dict):
            continue
        line = _line_number(item.get("line"), line_count)
        purpose = _text(item.get("purpose"))
        if line is None or not purpose:
            continue
        out.append({"line": line, "purpose": purpose})
    return out


def _json_safe(value: object, depth: int = 0) -> object:
    """Recursively keep only plain JSON containers/scalars - drops anything
    (functions can't appear post-json.loads, but absurdly deep nesting or a
    stray non-primitive) that isn't safe to hand straight to the renderer."""
    if depth > 6:
        return None
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, list):
        return [_json_safe(v, depth + 1) for v in value[:500]]
    if isinstance(value, dict):
        return {str(k)[:80]: _json_safe(v, depth + 1) for k, v in list(value.items())[:100]}
    return None


def _simulation_model(value: object) -> Dict:
    if not isinstance(value, dict):
        return {"type": "none", "initialState": None}
    kind = str(value.get("type") or "none").strip().lower()
    if kind not in SIMULATION_TYPES:
        kind = "none"
    state = value.get("initialState")
    if not isinstance(state, (list, dict)):
        state = None
    return {"type": kind, "initialState": _json_safe(state)}


def _steps(value: object, line_count: int) -> List[Dict]:
    if not isinstance(value, list):
        return []
    out = []
    for item in value[:MAX_STEPS]:
        if not isinstance(item, dict):
            continue
        op = str(item.get("op") or "").strip().lower()
        if op not in STEP_OPS:
            continue
        step = {"op": op}
        indices = item.get("indices")
        if isinstance(indices, list):
            clean = [int(i) for i in indices[:4] if isinstance(i, (int, float)) and not isinstance(i, bool)]
            if clean:
                step["indices"] = clean
        for key in ("node", "from", "to"):
            if key in item and isinstance(item[key], (str, int, float)) and not isinstance(item[key], bool):
                step[key] = item[key] if isinstance(item[key], str) else int(item[key])
        if "value" in item and isinstance(item["value"], (str, int, float)) and not isinstance(item["value"], bool):
            step["value"] = item["value"]
        if "weight" in item and isinstance(item["weight"], (int, float)) and not isinstance(item["weight"], bool):
            step["weight"] = item["weight"]
        line = _line_number(item.get("line"), line_count)
        if line is not None:
            step["line"] = line
        out.append(step)
    return out


def parse_blueprint_response(raw_text: str, line_count: int = 0) -> Dict:
    """Never raises. A response that fails validation renders as the
    "not identified" / empty-timeline defaults rather than erroring the tab."""
    data = _extract_json(raw_text) or {}
    return {
        "detectedAlgorithm": _text(data.get("detectedAlgorithm"), "Not identified"),
        "paradigm": _text(data.get("paradigm"), "Unknown"),
        "timeComplexity": _text(data.get("timeComplexity"), "Unknown"),
        "spaceComplexity": _text(data.get("spaceComplexity"), "Unknown"),
        "criticalLines": _critical_lines(data.get("criticalLines"), line_count),
        "simulationModel": _simulation_model(data.get("simulationModel")),
        "steps": _steps(data.get("steps"), line_count),
    }

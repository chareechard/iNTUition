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
import math
import re
from typing import Dict, List, Optional

MAX_CRITICAL_LINES = 8
MAX_STEPS = 300
MAX_SIMULATION_ITEMS = 200
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
    if isinstance(value, float) and not math.isfinite(value):
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
    if kind == "array":
        # The canvas only has a meaningful array frame when it can replay a
        # bounded list of scalar cells. In particular, do not let a model
        # hand us nested objects that render as zero and look like a real run.
        if (not isinstance(state, list) or not state or
                len(state) > MAX_SIMULATION_ITEMS or
                any(isinstance(v, bool) or not isinstance(v, (int, float, str))
                    or isinstance(v, float) and not math.isfinite(v)
                    for v in state)):
            return {"type": "none", "initialState": None}
    elif kind in ("graph", "tree"):
        if not isinstance(state, dict):
            return {"type": "none", "initialState": None}
        nodes = state.get("nodes")
        edges = state.get("edges", [])
        if (not isinstance(nodes, list) or not nodes or len(nodes) > MAX_SIMULATION_ITEMS or
                not isinstance(edges, list) or len(edges) > MAX_SIMULATION_ITEMS * 2):
            return {"type": "none", "initialState": None}
    elif not isinstance(state, (list, dict)):
        state = None
    if state is None:
        return {"type": "none", "initialState": None}
    return {"type": kind, "initialState": _json_safe(state)}


def _integer_index(value: object) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _graph_ids(state: object) -> set:
    if not isinstance(state, dict) or not isinstance(state.get("nodes"), list):
        return set()
    ids = set()
    for node in state["nodes"]:
        if isinstance(node, dict):
            value = node.get("id", node.get("name"))
        else:
            value = node
        if isinstance(value, (str, int, float)) and not isinstance(value, bool):
            ids.add(str(value))
    return ids


def _graph_edges(state: object) -> set:
    if not isinstance(state, dict) or not isinstance(state.get("edges"), list):
        return set()
    edges = set()
    for edge in state["edges"]:
        if not isinstance(edge, dict):
            continue
        source, target = edge.get("from"), edge.get("to")
        if (isinstance(source, (str, int, float)) and not isinstance(source, bool) and
                isinstance(target, (str, int, float)) and not isinstance(target, bool)):
            edges.add((str(source), str(target)))
    return edges


def _steps(value: object, line_count: int, model: Dict) -> List[Dict]:
    if not isinstance(value, list):
        return []
    kind = model.get("type")
    state = model.get("initialState")
    if kind == "none":
        return []
    graph_ids = _graph_ids(state) if kind in ("graph", "tree") else set()
    graph_edges = _graph_edges(state) if kind in ("graph", "tree") else set()
    out = []
    for item in value[:MAX_STEPS]:
        if not isinstance(item, dict):
            continue
        op = str(item.get("op") or "").strip().lower()
        if op not in STEP_OPS:
            continue
        step = {"op": op}
        indices = item.get("indices")
        if kind == "array":
            if op not in ("compare", "swap", "set") or not isinstance(indices, list):
                continue
            required = 1 if op == "set" else 2
            if len(indices) != required:
                continue
            clean = [_integer_index(i) for i in indices]
            if (any(i is None or i < 0 or i >= len(state) for i in clean) or
                    (op == "set" and (
                        "value" not in item or
                        isinstance(item.get("value"), bool) or
                        not isinstance(item.get("value"), (int, float, str))))):
                continue
            step["indices"] = clean
        elif op == "visit":
            node = item.get("node")
            if (isinstance(node, bool) or not isinstance(node, (str, int, float)) or
                    str(node) not in graph_ids):
                continue
            step["node"] = node
        else:
            if op not in ("edge", "relax"):
                continue
            source, target = item.get("from"), item.get("to")
            if (isinstance(source, bool) or not isinstance(source, (str, int, float)) or
                    isinstance(target, bool) or not isinstance(target, (str, int, float)) or
                    str(source) not in graph_ids or str(target) not in graph_ids or
                    (str(source), str(target)) not in graph_edges):
                continue
            step["from"], step["to"] = source, target
            if "weight" in item:
                weight = item.get("weight")
                if (isinstance(weight, (int, float)) and not isinstance(weight, bool) and
                        (not isinstance(weight, float) or math.isfinite(weight))):
                    step["weight"] = weight
        line = _line_number(item.get("line"), line_count)
        if line is not None:
            step["line"] = line
        if kind == "array" and op == "set":
            step["value"] = item["value"]
        out.append(step)
    return out


def parse_blueprint_response(raw_text: str, line_count: int = 0) -> Dict:
    """Never raises. A response that fails validation renders as the
    "not identified" / empty-timeline defaults rather than erroring the tab."""
    data = _extract_json(raw_text) or {}
    simulation_model = _simulation_model(data.get("simulationModel"))
    return {
        "detectedAlgorithm": _text(data.get("detectedAlgorithm"), "Not identified"),
        "paradigm": _text(data.get("paradigm"), "Unknown"),
        "timeComplexity": _text(data.get("timeComplexity"), "Unknown"),
        "spaceComplexity": _text(data.get("spaceComplexity"), "Unknown"),
        "criticalLines": _critical_lines(data.get("criticalLines"), line_count),
        "simulationModel": simulation_model,
        "steps": _steps(data.get("steps"), line_count,
                         simulation_model),
    }

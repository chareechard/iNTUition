"""Turn FRIDAY's read of a graphed page into GeoGebra input, safely.

The model sees a screenshot of whatever page the student currently has open and
is asked to report the function(s) it names, in strict JSON. That response is
untrusted the same way any vision answer is: the source page can be any
document in the user's Drive, and the browser feeds whatever comes back
straight into GeoGebra's ``evalCommand`` scripting. Nothing here is treated as
safe until it matches the allowlist below - an expression that is not plain
algebra in x/y is dropped, never passed through.
"""
import json
import re
from typing import Dict, List, Optional

MAX_FUNCTIONS = 4
BOUND_LIMIT = 200.0

# A small, closed vocabulary. The letters GeoGebra could interpret as a
# command name (Execute, SetValue, ...) are never in this list, so an
# expression naming one is rejected outright rather than sanitised.
FUNCTION_NAMES = ("sin", "cos", "tan", "asin", "acos", "atan", "sinh", "cosh",
                  "tanh", "sqrt", "exp", "ln", "log", "abs", "floor", "ceil",
                  "pi", "e")
_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_REMAINDER_ALLOWED = re.compile(r"^[0-9.,()+\-*/^\s]*$")


def _safe_expression(expr: object) -> Optional[str]:
    """``expr`` unchanged if it is plain algebra in x/y, else ``None``.

    Every letter-led token is checked against the allowlist first; whatever
    is left after stripping those tokens out must be nothing but digits,
    operators, grouping and whitespace. A semicolon, a stray identifier, or
    anything GeoGebra would parse as a second command fails that second
    check even if it never looked like a function name.
    """
    expr = str(expr or "").strip()
    if not expr or len(expr) > 200:
        return None
    for word in _WORD.findall(expr):
        if word not in ("x", "y") and word.lower() not in FUNCTION_NAMES:
            return None
    remainder = _WORD.sub("", expr)
    if not _REMAINDER_ALLOWED.match(remainder):
        return None
    return expr


def _safe_vars(vars_: object) -> Optional[List[str]]:
    cleaned = [str(v).strip() for v in (vars_ or ["x"])] if isinstance(vars_, list) else None
    if cleaned == ["x"]:
        return ["x"]
    if cleaned == ["x", "y"]:
        return ["x", "y"]
    return None


def _safe_bound(pair: object, default: List[float]) -> List[float]:
    try:
        lo, hi = float(pair[0]), float(pair[1])
    except (TypeError, ValueError, IndexError, KeyError):
        return default
    lo, hi = max(-BOUND_LIMIT, lo), min(BOUND_LIMIT, hi)
    if not lo < hi:
        return default
    return [lo, hi]


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


def parse_graph_response(raw_text: str) -> Dict:
    """Validate FRIDAY's reading of a page into a spec safe to hand to GeoGebra.

    Never raises. Anything that doesn't survive validation is dropped rather
    than passed through - the caller always gets a spec it can act on as-is.
    An empty ``functions`` list means "nothing graphable found or trusted",
    which covers both a page with no explicit function and a response that
    failed validation; the caller does not need to tell those apart.
    """
    data = _extract_json(raw_text) or {}
    raw_functions = data.get("functions")
    functions = []
    if isinstance(raw_functions, list):
        for item in raw_functions[:MAX_FUNCTIONS]:
            if not isinstance(item, dict):
                continue
            expr = _safe_expression(item.get("expr"))
            vars_ = _safe_vars(item.get("vars"))
            if expr and vars_:
                functions.append({"expr": expr, "vars": vars_})
    return {
        "functions": functions,
        "domain": _safe_bound(data.get("domain"), [-8.0, 8.0]),
        "range": _safe_bound(data.get("range"), [-8.0, 8.0]),
    }

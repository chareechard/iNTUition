"""Runtime tracing helper for the Lab's Python execution path.

This file is launched in a child interpreter and runs the student's source
with sys.settrace. It deliberately records a small, JSON-safe view of user
variables rather than trying to serialise arbitrary Python objects. The parent
process still owns stdout/stderr, so tracing never contaminates program output.
"""
import json
import math
import os
import runpy
import sys
from typing import Any, Dict, Iterable, List, Optional, Tuple


MAX_EVENTS = 400
MAX_ITEMS = 160
MAX_DEPTH = 5
MAX_STRING = 400


def _canonical(path: str) -> str:
    return os.path.normcase(os.path.realpath(path))


def _scalar(value: Any) -> bool:
    return value is None or isinstance(value, (bool, int, float, str))


def _safe(value: Any, depth: int = 0, seen: Optional[set] = None) -> Any:
    """Convert common algorithm state into bounded JSON-safe values."""
    if depth > MAX_DEPTH:
        return "<depth limit>"
    if seen is None:
        seen = set()
    if _scalar(value):
        if isinstance(value, float) and not math.isfinite(value):
            return str(value)
        if isinstance(value, str):
            return value[:MAX_STRING]
        return value
    identity = id(value)
    if identity in seen:
        return "<cycle>"
    seen.add(identity)
    try:
        if isinstance(value, (list, tuple)):
            return [_safe(item, depth + 1, seen) for item in list(value)[:MAX_ITEMS]]
        if isinstance(value, (set, frozenset)):
            values = [_safe(item, depth + 1, seen) for item in list(value)[:MAX_ITEMS]]
            return sorted(values, key=lambda item: repr(item))
        if isinstance(value, dict):
            result = {}
            for key, item in list(value.items())[:MAX_ITEMS]:
                result[str(key)[:MAX_STRING]] = _safe(item, depth + 1, seen)
            return result
        return repr(value)[:MAX_STRING]
    finally:
        seen.discard(identity)


def _flat_sequence(value: Any) -> bool:
    return (isinstance(value, list) and bool(value) and
            all(_scalar(item) for item in value))


def _state_kind(name: str, value: Any, input_kind: str) -> Optional[str]:
    name = name.lower()
    if (isinstance(value, dict) and isinstance(value.get("nodes"), list) and
            isinstance(value.get("edges"), list)):
        return "graph"
    if (isinstance(value, list) and value and
            all(isinstance(row, list) for row in value)):
        return "matrix"
    if _flat_sequence(value):
        if input_kind == "tree" or "tree" in name:
            return "tree"
        return "array"
    if isinstance(value, str) and input_kind == "string":
        return "text"
    return None


def _candidate_score(name: str, kind: str, value: Any, input_kind: str) -> int:
    lowered = name.lower()
    score = len(value) if hasattr(value, "__len__") else 1
    if lowered in {"values", "value", "array", "arr", "data", "matrix", "graph", "tree", "items", "nums"}:
        score += 1000
    if input_kind and input_kind == kind:
        score += 500
    if kind == "array" and any(token in lowered for token in ("result", "output", "answer")):
        score -= 120
    return score


class RuntimeTracer:
    def __init__(self, target_path: str, input_kind: str):
        self.target_path = _canonical(target_path)
        self.input_kind = input_kind
        self.events: List[Dict[str, Any]] = []
        self.primary_name: Optional[str] = None
        self.primary_kind: Optional[str] = None
        self.last_state: Any = None
        self.last_line: Dict[int, int] = {}
        self.truncated = False

    def _candidates(self, frame) -> Iterable[Tuple[int, str, str, Any]]:
        current = frame
        distance = 0
        while current is not None:
            if _canonical(current.f_code.co_filename) != self.target_path:
                current = current.f_back
                distance += 1
                continue
            for name, value in current.f_locals.items():
                kind = _state_kind(str(name), value, self.input_kind)
                if kind:
                    yield (_candidate_score(str(name), kind, value, self.input_kind) - distance,
                           str(name), kind, _safe(value))
            current = current.f_back
            distance += 1

    def _state(self, frame) -> Any:
        candidates = list(self._candidates(frame))
        selected = None
        if self.primary_name:
            selected = next((item for item in candidates if item[1] == self.primary_name), None)
        if selected is None and candidates:
            selected = max(candidates, key=lambda item: item[0])
        if selected is None:
            return self.last_state
        _, name, kind, value = selected
        self.primary_name = name
        self.primary_kind = kind
        self.last_state = value
        return value

    def _record(self, frame, line: int, phase: str) -> None:
        if len(self.events) >= MAX_EVENTS:
            self.truncated = True
            return
        state = self._state(frame)
        event = {"line": int(line), "phase": phase}
        if state is not None:
            event["state"] = state
        self.events.append(event)

    def __call__(self, frame, event: str, arg):
        if _canonical(frame.f_code.co_filename) != self.target_path:
            return None
        frame_id = id(frame)
        if event == "call":
            self.last_line[frame_id] = frame.f_lineno
            return self
        if event == "line":
            self.last_line[frame_id] = frame.f_lineno
            self._record(frame, frame.f_lineno, "line")
            return self
        if event == "return":
            self._record(frame, self.last_line.get(frame_id, frame.f_lineno), "return")
            self.last_line.pop(frame_id, None)
            return self
        return self

    def payload(self) -> Dict[str, Any]:
        states = [event.get("state") for event in self.events if "state" in event]
        initial = states[0] if states else None
        final = states[-1] if states else None
        kind = self.primary_kind if initial is not None else None
        return {
            "version": 1,
            "inputKind": self.input_kind,
            "model": {
                "type": kind or "none",
                "initialState": initial,
                "finalState": final,
            },
            "frames": self.events,
            "truncated": self.truncated,
        }


def main() -> int:
    if len(sys.argv) != 4:
        raise SystemExit("usage: lab_runtime.py <source.py> <trace.json> <input-kind>")
    source_path, trace_path, input_kind = sys.argv[1:]
    tracer = RuntimeTracer(source_path, input_kind)
    sys.argv = [source_path]
    sys.settrace(tracer)
    try:
        runpy.run_path(source_path, run_name="__main__")
    finally:
        sys.settrace(None)
        with open(trace_path, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(tracer.payload(), handle, ensure_ascii=True, separators=(",", ":"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


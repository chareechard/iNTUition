"""Runtime tracing helper for the Lab's Python execution path.

This file is launched in a child interpreter and runs the student's source
with sys.settrace. It deliberately records a small, JSON-safe view of user
variables rather than trying to serialise arbitrary Python objects. The parent
process still owns stdout/stderr, so tracing never contaminates program output.

Each recorded frame is a code-by-code snapshot: the source line, the phase
("call"/"line"/"return"/"exception"), the function it is in, the call stack of
user frames, an activation id so the UI can tell one call of a function from the
next, and a bounded name->value view of that frame's locals. Return values and
exception messages are captured too, so the runthrough matches what the code
actually did.

A single "primary structure" ("state"/"k") also rides along for the Simulation
canvas. It is chosen per frame from the live locals, preferring the thing the
algorithm is actually mutating, and recognises the shapes a DSA course leans on:
array, matrix, map/table, graph (incl. adjacency map/list), tree (node objects
or a heap array), and singly linked lists. ``collections.deque``, ``range`` and
NumPy arrays are read as their sequence form, and a thin wrapper object
(``Stack().items``, ``Graph().adj``, ``LinkedList().head`` …) is peeled down to
the collection it holds so a student's own class still animates.
"""
import functools
import json
import math
import os
import runpy
import sys
import types
from typing import Any, Dict, Iterable, List, Optional, Tuple


MAX_EVENTS = 1500
MAX_ITEMS = 200
MAX_DEPTH = 5
MAX_STRING = 400
MAX_LOCALS = 40
MAX_STACK = 24

_MISSING = object()

# Names bound to these are almost never the "data" a student is stepping
# through - they are imports, helper functions, and class definitions that
# would only clutter the variables panel.
_SKIP_LOCAL_TYPES = (types.ModuleType, types.FunctionType, types.BuiltinFunctionType,
                     types.MethodType, types.GeneratorType, type)

# Locals a graph/tree traversal typically accumulates as it runs; used to light
# up the "reached so far" nodes on the Simulation canvas.
_PROGRESS_NAMES = frozenset({
    "visited", "seen", "discovered", "explored", "visited_set", "closed",
    "path", "order", "traversal", "result", "component", "reachable",
    "stack", "queue", "frontier", "used", "marked",
})

# Attributes that carry a node's payload value / its successor pointer.
_VALUE_ATTRS = ("val", "value", "data", "key", "item", "info", "payload")
_NEXT_ATTRS = ("next", "nxt", "next_node", "succ")

# Names that strongly imply a particular structure, so the primary picker does
# not have to guess from shape alone.
_STRUCTURE_NAMES = frozenset({
    "values", "value", "array", "arr", "data", "matrix", "grid", "board",
    "graph", "tree", "heap", "items", "nums", "dp", "memo", "dist", "distance",
    "distances", "adj", "head", "root", "nodes", "list", "table", "cache",
    "stack", "queue", "deque", "dq", "pq", "q", "priority_queue",
    "buffer", "bucket", "buckets",
    "adjacency", "neighbours", "neighbors", "children", "elements",
})
# Function-name tokens that unambiguously mark heap code, regardless of what
# the array parameter itself is called (``min_heapify(nums, i)`` and friends).
_HEAP_FUNC_NAMES = (
    "heapify", "heapsort", "heap_sort", "siftup", "sift_up", "siftdown",
    "sift_down", "bubbleup", "bubble_up", "bubbledown", "bubble_down",
    "percolate", "build_heap", "buildheap", "min_heap", "max_heap",
    "minheap", "maxheap", "heappush", "heappop",
)
_TABLE_NAMES = frozenset({
    "dp", "memo", "dist", "distance", "distances", "cache", "count", "counts",
    "freq", "frequency", "parent", "prev", "seen", "indegree", "degree",
    "color", "colour", "rank", "low", "disc", "component", "comp",
    "label", "labels",
})

# Attribute names a wrapper class hangs its backing collection off of, tried
# first when peeling ``self`` / a wrapper instance down to real data.
_BACKING_ATTRS = ("items", "data", "elements", "values", "arr", "array",
                  "stack", "queue", "heap", "buffer", "buf", "store", "table",
                  "adj", "adjacency", "graph", "nodes", "head", "root", "list")


@functools.lru_cache(maxsize=256)
def _canonical(path: str) -> str:
    # Called on every trace event (sometimes more than once per event, while
    # walking the frame chain), so the realpath() syscall underneath it is on
    # the hot path for every line of the student's program. Frame filenames
    # are drawn from a tiny, fixed set per run (the target file, occasionally
    # a stdlib module), so caching turns millions of syscalls into a handful.
    return os.path.normcase(os.path.realpath(path))


def _scalar(value: Any) -> bool:
    return value is None or isinstance(value, (bool, int, float, str))


def _hasattr(obj: Any, name: str) -> bool:
    try:
        return hasattr(obj, name)
    except Exception:  # noqa: BLE001 - student __getattr__ can raise anything
        return False


def _getattr(obj: Any, name: str, default: Any = None) -> Any:
    try:
        return getattr(obj, name, default)
    except Exception:  # noqa: BLE001
        return default


def _is_deque(value: Any) -> bool:
    return (type(value).__name__ == "deque" and _hasattr(value, "popleft")
            and _hasattr(value, "append"))


def _seqish(value: Any) -> bool:
    """A concrete, bounded, index/iteration-friendly container."""
    return isinstance(value, (list, tuple, set, frozenset, dict)) or _is_deque(value)


def _numpyish_list(value: Any) -> Any:
    """A NumPy array / scalar rendered as the plain list / number it stands for,
    so ``np.zeros((3, 3))`` animates as a matrix rather than a repr string."""
    module = (getattr(type(value), "__module__", "") or "").split(".")[0]
    if module != "numpy":
        return value
    if _hasattr(value, "tolist"):
        size = _getattr(value, "size")
        if isinstance(size, int) and size > MAX_ITEMS * MAX_ITEMS:
            return repr(value)[:MAX_STRING]
        try:
            return value.tolist()
        except Exception:  # noqa: BLE001
            return value
    if _hasattr(value, "item"):
        try:
            return value.item()
        except Exception:  # noqa: BLE001
            return value
    return value


def _as_sequence(value: Any) -> Any:
    """Fold the sequence-likes a student reaches for - ``collections.deque``,
    ``range`` and NumPy arrays - into a plain list; anything else is unchanged."""
    if _is_deque(value):
        return list(value)[:MAX_ITEMS + 1]
    if isinstance(value, range):
        try:
            return list(value)[:MAX_ITEMS + 1]
        except (OverflowError, MemoryError, ValueError):
            return value
    return _numpyish_list(value)


def _obj_attrs(value: Any) -> Optional[Dict[str, Any]]:
    """Public instance attributes of a plain object - from ``__dict__`` or,
    failing that, ``__slots__`` - or None when ``value`` is not such an object."""
    try:
        raw: Optional[Dict[str, Any]] = dict(vars(value))
    except TypeError:
        raw = None
    if raw is None:
        slots = getattr(type(value), "__slots__", None)
        if isinstance(slots, str):
            slots = (slots,)
        if not slots:
            return None
        raw = {}
        for name in list(slots)[:MAX_LOCALS]:
            try:
                raw[str(name)] = getattr(value, name)
            except Exception:  # noqa: BLE001
                continue
    return {name: item for name, item in raw.items()
            if not str(name).startswith("_")}


def _small(value: Any) -> bool:
    try:
        return len(value) <= 24
    except TypeError:
        return False


def _object_fields(value: Any, depth: int = 0,
                   seen: Optional[set] = None) -> Optional[Dict[str, Any]]:
    """A plain record / dataclass / node object rendered as its public data:
    scalar attributes verbatim, plus one shallow level of small scalar
    collections, so it reads as data rather than ``<Foo object at 0x...>``."""
    attrs = _obj_attrs(value)
    if attrs is None:
        return None
    fields: Dict[str, Any] = {}
    for name, item in list(attrs.items())[:MAX_ITEMS]:
        if _scalar(item):
            fields[name] = item[:MAX_STRING] if isinstance(item, str) else item
        elif depth < MAX_DEPTH and _seqish(_as_sequence(item)) and _small(item):
            fields[name] = _safe(_as_sequence(item), depth + 1, seen)
    return fields or None


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
            return value if len(value) <= MAX_STRING else value[:MAX_STRING] + "…"
        return value
    coerced = _as_sequence(value)
    if coerced is not value:
        return _safe(coerced, depth, seen)
    identity = id(value)
    if identity in seen:
        return "<cycle>"
    seen.add(identity)
    try:
        if isinstance(value, (list, tuple)):
            out = [_safe(item, depth + 1, seen) for item in list(value)[:MAX_ITEMS]]
            if len(value) > MAX_ITEMS:
                out.append("…+{}".format(len(value) - MAX_ITEMS))
            return out
        if isinstance(value, (set, frozenset)):
            values = [_safe(item, depth + 1, seen) for item in list(value)[:MAX_ITEMS]]
            return sorted(values, key=lambda item: repr(item))
        if isinstance(value, dict):
            result = {}
            for key, item in list(value.items())[:MAX_ITEMS]:
                result[str(key)[:MAX_STRING]] = _safe(item, depth + 1, seen)
            if len(value) > MAX_ITEMS:
                result["…"] = "+{} more".format(len(value) - MAX_ITEMS)
            return result
        fields = _object_fields(value, depth, seen)
        if fields:
            return fields
        return repr(value)[:MAX_STRING]
    finally:
        seen.discard(identity)


def _locals_snapshot(frame) -> Dict[str, Any]:
    """A bounded, JSON-safe name->value view of one frame's locals.

    Dunder names, imported modules, and helper functions/classes are skipped so
    the Simulation tab's variables panel shows the data the student is actually
    moving through, not scaffolding.
    """
    out: Dict[str, Any] = {}
    for name, value in list(frame.f_locals.items()):
        if len(out) >= MAX_LOCALS:
            break
        if name.startswith("__") and name.endswith("__"):
            continue
        if isinstance(value, _SKIP_LOCAL_TYPES):
            continue
        out[str(name)[:MAX_STRING]] = _safe(value)
    return out


# ── Structure recognition ────────────────────────────────────────────────────

def _flat_sequence(value: Any) -> bool:
    return (isinstance(value, list) and bool(value) and
            all(_scalar(item) for item in value))


def _scalar_collection(value: Any) -> bool:
    return (isinstance(value, (list, tuple, set, frozenset)) and
            all(_scalar(item) for item in value))


def _weighted_edges(neighbours: Any) -> Optional[List[Tuple[Any, Optional[float]]]]:
    """A neighbour list in either style a course leans on: plain scalars
    (``['B', 'C']``) or ``(neighbour, weight)`` / ``[neighbour, weight]`` pairs
    (``[('B', 1), ('C', 4)]`` - the shape ``for neighbour, weight in graph[v]``
    unpacks). Returns ``[(neighbour, weight-or-None), ...]``, or None when
    ``neighbours`` is neither."""
    if not isinstance(neighbours, (list, tuple, set, frozenset)):
        return None
    out: List[Tuple[Any, Optional[float]]] = []
    for item in list(neighbours)[:MAX_ITEMS]:
        if _scalar(item):
            out.append((item, None))
        elif (isinstance(item, (list, tuple)) and len(item) == 2 and _scalar(item[0])
              and isinstance(item[1], (int, float)) and not isinstance(item[1], bool)):
            out.append((item[0], item[1]))
        else:
            return None
    return out


def _adjacency(value: Any) -> Optional[Dict[str, List]]:
    """A graph written the way students usually write one ->
    ``{"nodes": [...], "edges": [{"from","to"[,"weight"]}]}``, else None.

    Handles an adjacency map ``{node: [neighbours]}``, a weighted map
    ``{node: {neighbour: weight}}``, a weighted adjacency list
    ``{node: [(neighbour, weight), ...]}`` (the ``heapq`` Dijkstra idiom), and
    the index-addressed equivalents ``[[neighbours], ...]`` /
    ``[[(neighbour, weight), ...], ...]``. The dashboard's built-in
    ``{"nodes","edges"}`` shape never reaches here.
    """
    nodes: List[Any] = []
    edges: List[Dict[str, Any]] = []
    if isinstance(value, dict):
        items = list(value.items())
        if not items or len(items) > MAX_ITEMS:
            return None
        for key, neighbours in items:
            if not _scalar(key):
                return None
            nodes.append(key)
            if isinstance(neighbours, dict):
                for nbr, weight in list(neighbours.items())[:MAX_ITEMS]:
                    if not _scalar(nbr):
                        return None
                    edge: Dict[str, Any] = {"from": key, "to": nbr}
                    if isinstance(weight, (int, float)) and not isinstance(weight, bool):
                        edge["weight"] = weight
                    edges.append(edge)
            else:
                pairs = _weighted_edges(neighbours)
                if pairs is None:
                    return None
                for nbr, weight in pairs:
                    edge = {"from": key, "to": nbr}
                    if weight is not None:
                        edge["weight"] = weight
                    edges.append(edge)
    elif isinstance(value, list) and value:
        rows = [_weighted_edges(row) for row in value[:MAX_ITEMS]]
        if any(row is None for row in rows):
            return None
        for index, pairs in enumerate(rows):
            nodes.append(index)
            for nbr, weight in pairs:
                edge = {"from": index, "to": nbr}
                if weight is not None:
                    edge["weight"] = weight
                edges.append(edge)
    else:
        return None
    seen = {str(node) for node in nodes}
    for edge in edges:
        if str(edge["to"]) not in seen:
            seen.add(str(edge["to"]))
            nodes.append(edge["to"])
    return {"nodes": nodes[:MAX_ITEMS], "edges": edges[:MAX_ITEMS * 4]}


def _scalar_map(value: Any) -> Optional[Dict[str, List]]:
    """A dict used as a table - scalar keys to scalar values (counts, memo,
    distances, parent pointers). Returns ``{"pairs": [[k, v], ...]}`` or None."""
    if not isinstance(value, dict) or not value or len(value) > MAX_ITEMS:
        return None
    pairs = []
    for key, item in list(value.items()):
        if not _scalar(key) or not _scalar(item):
            return None
        pairs.append([_safe(key), _safe(item)])
    return {"pairs": pairs}


def _node_label(obj: Any) -> str:
    for attr in _VALUE_ATTRS:
        if _hasattr(obj, attr):
            candidate = _getattr(obj, attr)
            if _scalar(candidate):
                return str(candidate)[:40]
    return type(obj).__name__[:24]


def _linked_list(value: Any) -> Optional[Dict[str, List]]:
    """A singly linked list reachable through ``.next`` -> ``{"cells": [...]}``."""
    if _scalar(value) or isinstance(value, (list, tuple, dict, set, frozenset)):
        return None
    next_attr = next((a for a in _NEXT_ATTRS if _hasattr(value, a)), None)
    if next_attr is None:
        return None
    cells: List[Any] = []
    seen: set = set()
    node = value
    while node is not None and len(cells) < MAX_ITEMS:
        if id(node) in seen:
            cells.append("…cycle")
            break
        seen.add(id(node))
        cells.append(_node_label(node))
        node = _getattr(node, next_attr)
    if len(cells) < 2:
        return None
    return {"cells": cells}


def _heap_tree(value: Any) -> Optional[Dict[str, List]]:
    """A binary-heap array - ``heapq``'s own representation, or any list kept
    in implicit heap order - folded into ``{"nodes","edges","roots"}`` via the
    standard index ``i`` -> children ``2i+1, 2i+2`` relation, so it draws as a
    tree instead of a bar chart. Each slot is labelled by its scalar value, or
    by the leading fields of a ``(priority, item, ...)`` tuple - the
    ``heapq.heappush(pq, (dist, node))`` priority-queue idiom - so a Dijkstra/
    Prim priority queue still animates. None when ``value`` is not such a list."""
    if not isinstance(value, list) or len(value) < 2:
        return None
    labels: List[str] = []
    for item in value[:MAX_ITEMS]:
        if _scalar(item):
            labels.append(str(item)[:24])
        elif isinstance(item, (list, tuple)) and item and _scalar(item[0]):
            labels.append(",".join(str(part)[:12] for part in item[:3]))
        else:
            return None
    nodes = [{"id": str(i), "label": labels[i]} for i in range(len(labels))]
    edges = [{"from": str(i), "to": str(child)}
             for i in range(len(labels))
             for child in (2 * i + 1, 2 * i + 2) if child < len(labels)]
    return {"nodes": nodes, "edges": edges, "roots": ["0"]}


def _tree_object(value: Any) -> Optional[Dict[str, List]]:
    """A binary / n-ary tree of node objects -> ``{"nodes","edges","roots"}``."""
    if _scalar(value) or isinstance(value, (list, tuple, dict, set, frozenset)):
        return None
    if not any(_hasattr(value, a) for a in ("left", "right", "children")):
        return None
    nodes: List[Dict[str, Any]] = [{"id": "0", "label": _node_label(value)}]
    edges: List[Dict[str, Any]] = []
    idmap = {id(value): "0"}
    order = [value]
    head = 0
    while head < len(order) and len(nodes) < MAX_ITEMS:
        node = order[head]
        head += 1
        pid = idmap[id(node)]
        pairs: List[Tuple[str, Any]] = []
        for side in ("left", "right"):
            child = _getattr(node, side)
            if child is not None and not _scalar(child):
                pairs.append((side, child))
        kids = _getattr(node, "children")
        if isinstance(kids, (list, tuple)):
            pairs.extend(("child", c) for c in kids if c is not None and not _scalar(c))
        for side, child in pairs:
            existing = idmap.get(id(child))
            if existing is not None:
                edges.append({"from": pid, "to": existing, "side": side})
                continue
            cid = str(len(idmap))
            idmap[id(child)] = cid
            nodes.append({"id": cid, "label": _node_label(child)})
            edges.append({"from": pid, "to": cid, "side": side})
            order.append(child)
    if len(nodes) < 2:
        return None
    return {"nodes": nodes, "edges": edges, "roots": ["0"]}


def _unwrap(value: Any) -> Tuple[Any, Optional[str]]:
    """Peel a thin wrapper object down to the structure it holds - ``Stack().items``,
    ``Graph().adj``, ``LinkedList().head``, ``Tree().root`` - so a student's own
    class animates like the built-in it wraps. Returns ``(inner, attr_name)``, or
    ``(value, None)`` when there is nothing to peel. Bounded to a few hops, and it
    stops the moment the object is itself a recognised structure so a linked-list
    or tree node is never walked into."""
    label: Optional[str] = None
    for _ in range(3):
        if _scalar(value) or _seqish(_as_sequence(value)):
            return value, label
        attrs = _obj_attrs(value)
        if not attrs:
            return value, label
        named = [(n, attrs[n]) for n in _BACKING_ATTRS
                 if n in attrs and attrs[n] is not None and not _scalar(attrs[n])]
        nonscalar = named or [(n, v) for n, v in attrs.items()
                              if v is not None and not _scalar(v)]
        if len(nonscalar) != 1:
            return value, label
        if _linked_list(value) is not None or _tree_object(value) is not None:
            return value, label
        label = label or str(nonscalar[0][0])
        value = nonscalar[0][1]
    return value, label


def _row_matrix(value: Any) -> bool:
    """``value`` reads as a 2-D grid: a list whose entries are rows (lists),
    tolerating a few ``None`` / not-yet-filled rows so a DP table still counts
    while it is being built up one row at a time."""
    if not isinstance(value, list) or not value:
        return False
    rows = [row for row in value if row is not None]
    if not rows or any(not isinstance(row, list) for row in rows):
        return False
    return len(rows) * 2 >= len(value)


def _state_kind(name: str, value: Any, input_kind: str, func_name: str = "") -> Optional[str]:
    lname = name.lower()
    value = _as_sequence(value)
    graph_hint = (input_kind == "graph" or
                  any(token in lname for token in
                      ("graph", "adj", "edges", "neighbour", "neighbor")))
    if (isinstance(value, dict) and isinstance(value.get("nodes"), list) and
            isinstance(value.get("edges"), list)):
        return "graph"
    if isinstance(value, dict) and _adjacency(value) is not None:
        return "graph"
    if isinstance(value, dict) and _scalar_map(value) is not None:
        return "map"
    if _row_matrix(value):
        # A list of lists is shape-identical to a grid, so only read it as a
        # graph when the name or the declared input says so.
        if graph_hint and _adjacency(value) is not None:
            return "graph"
        return "matrix"
    # A course's own heap code rarely names the array "heap" (SC2001's own
    # ``min_heapify(nums, i)`` calls it ``nums``, like any other array) - so
    # also read it from the enclosing function, which a heap routine names
    # unambiguously even when the array itself does not.
    heap_name = ("heap" in lname or lname in ("pq", "priority_queue", "priorityqueue") or
                 any(token in func_name.lower() for token in _HEAP_FUNC_NAMES))
    if isinstance(value, list) and (heap_name or input_kind == "tree" or "tree" in lname) \
            and _heap_tree(value) is not None:
        return "tree"
    if _flat_sequence(value):
        return "array"
    if isinstance(value, str) and len(value) > 1 and (
            input_kind == "string" or
            any(token in lname for token in ("text", "str", "pattern", "word"))):
        return "text"
    if _linked_list(value) is not None:
        return "linked"
    if _tree_object(value) is not None:
        return "tree"
    return None


def _normalize_state(kind: str, value: Any) -> Any:
    """Fold a student's structure into the shape the canvas draws."""
    if kind == "graph":
        if not (isinstance(value, dict) and isinstance(value.get("nodes"), list)):
            adjacency = _adjacency(value)
            if adjacency is not None:
                return adjacency
    elif kind == "map":
        table = _scalar_map(value)
        if table is not None:
            return table
    elif kind == "linked":
        chain = _linked_list(value)
        if chain is not None:
            return chain
    elif kind == "tree":
        if isinstance(value, list):
            heap = _heap_tree(value)
            if heap is not None:
                return heap
        else:
            tree = _tree_object(value)
            if tree is not None:
                return tree
    return value


def _candidate_score(name: str, kind: str, value: Any, input_kind: str) -> int:
    lowered = name.lower()
    try:
        size = len(value)
    except TypeError:
        size = 1
    score = min(size, 200)
    if lowered in _STRUCTURE_NAMES:
        score += 1000
    if input_kind and input_kind == kind:
        score += 500
    if kind == "map" and lowered in _TABLE_NAMES:
        score += 400
    if kind == "array" and lowered in ("result", "output", "answer", "res", "ans", "acc"):
        # Prefer the input being transformed over a bare accumulator, but only
        # as a tie-breaker - mutation and naming can still win it back.
        score -= 90
    return score


# ── Tracer ───────────────────────────────────────────────────────────────────

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
        self._activations: Dict[int, int] = {}
        self._activation_seq = 0
        self._change_counts: Dict[str, int] = {}
        self._last_serialized: Dict[str, Any] = {}
        # Candidates that changed on the *current* frame (reset every call to
        # ``_candidates``) - a freshness signal so a structure the run has just
        # moved on to can overtake one with more cumulative activity, without
        # waiting for ``_change_counts`` to catch up. See ``_state``.
        self._changed_this_frame: set = set()
        # How many consecutive frames the same candidate has topped the raw
        # score - a challenger only unseats the established primary once it
        # has led for a few frames running (or by a decisive one-shot margin),
        # so a single noisy frame cannot flicker the picture back and forth.
        self._streak_name: Optional[str] = None
        self._streak_count: int = 0

    # -- helpers --

    def _activation_id(self, frame) -> int:
        fid = id(frame)
        act = self._activations.get(fid)
        if act is None:
            self._activation_seq += 1
            act = self._activations[fid] = self._activation_seq
        return act

    def _stack(self, frame) -> List[str]:
        """Function names of the live user frames, outermost first."""
        names: List[str] = []
        current = frame
        while current is not None and len(names) < MAX_STACK:
            if _canonical(current.f_code.co_filename) == self.target_path:
                names.append(current.f_code.co_name)
            current = current.f_back
        names.reverse()
        return names

    def _candidates(self, frame) -> List[Tuple[int, str, str, Any]]:
        out: List[Tuple[int, str, str, Any]] = []
        self._changed_this_frame = set()
        current = frame
        distance = 0
        while current is not None:
            if _canonical(current.f_code.co_filename) != self.target_path:
                current = current.f_back
                distance += 1
                continue
            for raw_name, raw_value in list(current.f_locals.items()):
                if raw_name == "cls":
                    continue  # the class, not the structure under study
                value, peeled = _unwrap(raw_value)
                if raw_name == "self":
                    # A method works on self.<field>; surface that field rather
                    # than the receiver, and only when peeling actually found one.
                    if peeled is None or value is raw_value:
                        continue
                    name = peeled
                else:
                    name = raw_name
                value = _as_sequence(value)
                kind = _state_kind(str(name), value, self.input_kind, current.f_code.co_name)
                if not kind:
                    continue
                serialized = _safe(_normalize_state(kind, value))
                key = str(name)
                previous = self._last_serialized.get(key, _MISSING)
                if previous is not _MISSING and previous != serialized:
                    self._change_counts[key] = self._change_counts.get(key, 0) + 1
                    self._changed_this_frame.add(key)
                self._last_serialized[key] = serialized
                base = _candidate_score(key, kind, value, self.input_kind) - distance
                out.append((base, key, kind, serialized))
            current = current.f_back
            distance += 1
        return out

    def _highlight(self, frame) -> Optional[List[Any]]:
        """The set/sequence a graph or tree walk is filling in as it runs -
        ``visited``/``seen``/``frontier`` and friends - so the canvas can light
        up the nodes reached so far. Returns None when nothing fits."""
        best: Any = None
        best_score = 0
        current = frame
        while current is not None:
            if _canonical(current.f_code.co_filename) == self.target_path:
                for name, value in list(current.f_locals.items()):
                    lname = str(name).lower()
                    named = lname in _PROGRESS_NAMES
                    if isinstance(value, (set, frozenset)):
                        score = 3 + (2 if named else 0)
                    elif named and isinstance(value, list) and _flat_sequence(value):
                        score = 3
                    else:
                        continue
                    if score > best_score:
                        best, best_score = value, score
            current = current.f_back
        if best is None:
            return None
        return [_safe(item) for item in list(best)[:MAX_ITEMS] if _scalar(item)]

    def _state(self, frame) -> Any:
        candidates = self._candidates(frame)
        if not candidates:
            return self.last_state
        # Cumulative activity (capped, so an early flurry can't lock a name in
        # forever) plus a flat bonus for changing on *this* frame specifically -
        # a freshness signal, so a structure the run has just moved on to can
        # overtake one that was merely busier earlier, without waiting several
        # frames for the cumulative count to catch up.
        scored = sorted(
            ((base + 4 * min(self._change_counts.get(name, 0), 60)
                   + (140 if name in self._changed_this_frame else 0),
              name, kind, value)
             for base, name, kind, value in candidates),
            key=lambda item: item[0], reverse=True)

        top_name = scored[0][1]
        if top_name == self._streak_name:
            self._streak_count += 1
        else:
            self._streak_name = top_name
            self._streak_count = 1

        selected = None
        if self.primary_name:
            selected = next((s for s in scored if s[1] == self.primary_name), None)
            # Stay on the established primary unless another candidate has
            # now led the raw score for several frames running - one noisy
            # frame should never flip the picture - or is decisively more
            # relevant right now (a much larger margin clears it instantly,
            # e.g. the first real structure appearing where none existed).
            if selected is not None and top_name != selected[1] and \
                    (self._streak_count >= 3 or scored[0][0] > selected[0] + 350):
                selected = scored[0]
        if selected is None:
            selected = scored[0]

        _, name, kind, value = selected
        self.primary_name = name
        self.primary_kind = kind
        self.last_state = value
        return value

    # -- recording --

    def _record(self, frame, line: int, phase: str,
                extra: Optional[Dict[str, Any]] = None) -> None:
        if len(self.events) >= MAX_EVENTS:
            self.truncated = True
            return
        state = self._state(frame)
        stack = self._stack(frame)
        event: Dict[str, Any] = {
            "line": int(line),
            "phase": phase,
            "func": str(frame.f_code.co_name),
            "frame": self._activation_id(frame),
            "depth": len(stack),
            "stack": stack,
            "locals": _locals_snapshot(frame),
        }
        if state is not None:
            event["state"] = state
            event["k"] = self.primary_kind
            if self.primary_kind in ("graph", "tree"):
                highlight = self._highlight(frame)
                if highlight is not None:
                    event["highlight"] = highlight
        if extra:
            event.update(extra)
        self.events.append(event)

    def _continue(self) -> Optional["RuntimeTracer"]:
        # Once the event cap is hit, further frames/lines add nothing to the
        # trace - but sys.settrace still pays a Python-level call for each one.
        # For a loop that keeps running well past MAX_EVENTS (any O(n^2) sort
        # over a real array, say), that dead overhead can be the difference
        # between finishing and hitting the run's wall-clock timeout. Returning
        # None here drops the *local* trace for the current frame, and clearing
        # the global tracer stops new frames from being instrumented too, so
        # the rest of the run executes at native speed.
        if self.truncated:
            sys.settrace(None)
            return None
        return self

    def __call__(self, frame, event: str, arg):
        if _canonical(frame.f_code.co_filename) != self.target_path:
            return None
        frame_id = id(frame)
        if event == "call":
            # A module-level call reports f_lineno 0; land it on the first line.
            line = frame.f_lineno or 1
            self.last_line[frame_id] = line
            self._activation_id(frame)
            self._record(frame, line, "call")
            return self._continue()
        if event == "line":
            self.last_line[frame_id] = frame.f_lineno
            self._record(frame, frame.f_lineno, "line")
            return self._continue()
        if event == "return":
            extra = None
            if frame.f_code.co_name != "<module>":
                extra = {"ret": _safe(arg)}
            self._record(frame, self.last_line.get(frame_id, frame.f_lineno),
                         "return", extra)
            self.last_line.pop(frame_id, None)
            self._activations.pop(frame_id, None)
            return self._continue()
        if event == "exception":
            exc_type, exc_value = (None, None)
            if isinstance(arg, tuple) and arg:
                exc_type = arg[0]
                exc_value = arg[1] if len(arg) > 1 else None
            extra: Dict[str, Any] = {}
            if exc_type is not None:
                extra["exc"] = str(getattr(exc_type, "__name__", exc_type))[:120]
            if exc_value is not None:
                message = str(exc_value)[:MAX_STRING]
                if message:
                    extra["excMsg"] = message
            self._record(frame, frame.f_lineno, "exception", extra or None)
            return self._continue()
        return self

    def payload(self) -> Dict[str, Any]:
        states = [event.get("state") for event in self.events if "state" in event]
        initial = states[0] if states else None
        final = states[-1] if states else None
        kind = self.primary_kind if final is not None else None
        return {
            "version": 3,
            "inputKind": self.input_kind,
            "model": {
                "type": kind or "none",
                "initialState": initial,
                "finalState": final,
            },
            "frames": self.events,
            "eventCount": len(self.events),
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

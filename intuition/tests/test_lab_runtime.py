"""Unit tests for the Lab runtime tracer's structure recognition and its
bounded value rendering - the parts that decide what the Simulation tab draws
and what the variables panel shows. These poke the helpers directly so they do
not depend on which Python interpreter the JobManager happens to launch."""
from collections import deque

from intuition import lab_runtime as lr


# ── Sequence-likes fold to their list form ───────────────────────────────────

def test_deque_reads_as_an_array():
    assert lr._state_kind("queue", deque([3, 1, 4]), "none") == "array"
    assert lr._safe(deque([3, 1, 4])) == [3, 1, 4]


def test_range_reads_as_an_array():
    assert lr._state_kind("xs", range(5), "none") == "array"
    assert lr._safe(range(3)) == [0, 1, 2]


class _FakeNdArray:
    """Enough of the NumPy surface for the duck-typed path, without the dep."""
    __module__ = "numpy"

    def __init__(self, data):
        self._data = data

    def tolist(self):
        return self._data


def test_numpy_style_array_reads_as_matrix_or_array():
    grid = _FakeNdArray([[0, 0], [0, 0]])
    assert lr._state_kind("grid", grid, "none") == "matrix"
    assert lr._safe(grid) == [[0, 0], [0, 0]]
    row = _FakeNdArray([1, 2, 3])
    assert lr._state_kind("row", row, "none") == "array"


# ── Half-built 2-D tables still count as a grid ──────────────────────────────

def test_partly_filled_dp_table_is_a_matrix():
    assert lr._state_kind("dp", [[0, 0], [1, 1], None], "none") == "matrix"
    # A mostly-empty table is not a grid yet; a flat list is never one.
    assert lr._state_kind("dp", [[0, 0], None, None], "none") != "matrix"
    assert lr._state_kind("xs", [1, 2, 3], "none") == "array"
    assert lr._row_matrix([[1], [2], [3], None]) is True
    assert lr._row_matrix([[1], 2, 3]) is False


# ── Wrapper objects peel down to the collection they hold ────────────────────

class _Stack:
    def __init__(self, items):
        self.items = list(items)
        self.size = len(self.items)


class _Graph:
    def __init__(self):
        self.adj = {"A": ["B"], "B": []}
        self.directed = True


class _ListNode:
    def __init__(self, val, nxt=None):
        self.val = val
        self.next = nxt


class _LinkedList:
    def __init__(self, head):
        self.head = head


def test_wrapper_class_peels_to_its_backing_collection():
    value, attr = lr._unwrap(_Stack([3, 1, 4]))
    assert attr == "items" and value == [3, 1, 4]

    value, attr = lr._unwrap(_Graph())
    assert attr == "adj" and lr._state_kind(attr, value, "none") == "graph"


def test_wrapper_peels_through_to_a_linked_list_but_not_into_it():
    chain = _ListNode(1, _ListNode(2, _ListNode(3)))
    # A bare node is left alone for the linked-list recogniser.
    assert lr._unwrap(chain)[0] is chain
    # A one-field wrapper resolves to the node, still not past it.
    peeled, _ = lr._unwrap(_LinkedList(chain))
    assert peeled is chain
    assert lr._linked_list(peeled) == {"cells": ["1", "2", "3"]}


def test_ambiguous_object_is_not_peeled():
    class _TwoLists:
        def __init__(self):
            self.a = [1, 2]
            self.b = [3, 4]

    obj = _TwoLists()
    assert lr._unwrap(obj)[0] is obj


# ── Variables panel renders object state, not a repr string ──────────────────

def test_object_fields_include_shallow_collections():
    rendered = lr._safe(_Stack([1, 2, 3]))
    assert rendered == {"items": [1, 2, 3], "size": 3}


def test_slots_objects_still_render_as_data():
    class _Point:
        __slots__ = ("x", "y")

        def __init__(self):
            self.x, self.y = 1, 2

    assert lr._safe(_Point()) == {"x": 1, "y": 2}

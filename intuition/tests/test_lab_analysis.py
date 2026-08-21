import json

from intuition.lab_analysis import parse_blueprint_response


def _raw(**kwargs):
    return json.dumps(kwargs)


def test_a_clean_bubble_sort_blueprint_survives():
    blueprint = parse_blueprint_response(_raw(
        detectedAlgorithm="Bubble Sort", paradigm="Brute Force",
        timeComplexity="O(N^2)", spaceComplexity="O(1)",
        criticalLines=[{"line": 3, "purpose": "swap adjacent out-of-order elements"}],
        simulationModel={"type": "array", "initialState": [5, 3, 8, 1]},
        steps=[{"op": "compare", "indices": [0, 1], "line": 3},
               {"op": "swap", "indices": [0, 1], "line": 4}],
    ), line_count=10)
    assert blueprint["detectedAlgorithm"] == "Bubble Sort"
    assert blueprint["criticalLines"] == [{"line": 3, "purpose": "swap adjacent out-of-order elements"}]
    assert blueprint["simulationModel"] == {"type": "array", "initialState": [5, 3, 8, 1]}
    assert blueprint["steps"] == [{"op": "compare", "indices": [0, 1], "line": 3},
                                  {"op": "swap", "indices": [0, 1], "line": 4}]


def test_empty_or_garbage_response_falls_back_to_defaults():
    blueprint = parse_blueprint_response("not json at all")
    assert blueprint["detectedAlgorithm"] == "Not identified"
    assert blueprint["paradigm"] == "Unknown"
    assert blueprint["criticalLines"] == []
    assert blueprint["simulationModel"] == {"type": "none", "initialState": None}
    assert blueprint["steps"] == []


def test_critical_lines_beyond_the_file_are_dropped():
    blueprint = parse_blueprint_response(_raw(
        criticalLines=[{"line": 3, "purpose": "ok"}, {"line": 99, "purpose": "out of range"}],
    ), line_count=10)
    assert blueprint["criticalLines"] == [{"line": 3, "purpose": "ok"}]


def test_unknown_step_ops_and_simulation_types_are_dropped():
    blueprint = parse_blueprint_response(_raw(
        simulationModel={"type": "spreadsheet", "initialState": {"a": 1}},
        steps=[{"op": "teleport", "indices": [0]}, {"op": "swap", "indices": [1, 2]}],
    ))
    assert blueprint["simulationModel"]["type"] == "none"
    assert blueprint["steps"] == []


def test_array_steps_are_bounded_and_use_zero_based_cells():
    blueprint = parse_blueprint_response(_raw(
        simulationModel={"type": "array", "initialState": [4, 2, 1]},
        steps=[
            {"op": "compare", "indices": [0, 1]},
            {"op": "swap", "indices": [1, 2]},
            {"op": "set", "indices": [2], "value": 0},
            {"op": "swap", "indices": [3, 0]},
            {"op": "visit", "node": "0"},
        ],
    ))
    assert blueprint["steps"] == [
        {"op": "compare", "indices": [0, 1]},
        {"op": "swap", "indices": [1, 2]},
        {"op": "set", "indices": [2], "value": 0},
    ]


def test_markdown_fence_is_unwrapped():
    raw = "```json\n" + _raw(detectedAlgorithm="Binary Search") + "\n```"
    assert parse_blueprint_response(raw)["detectedAlgorithm"] == "Binary Search"


def test_graph_simulation_model_with_node_edge_steps():
    blueprint = parse_blueprint_response(_raw(
        detectedAlgorithm="Breadth-First Search",
        simulationModel={"type": "graph", "initialState": {
            "nodes": ["A", "B", "C"], "edges": [{"from": "A", "to": "B"}]}},
        steps=[{"op": "visit", "node": "A", "line": 5},
               {"op": "edge", "from": "A", "to": "B", "weight": 2}],
    ), line_count=20)
    assert blueprint["simulationModel"]["type"] == "graph"
    assert blueprint["steps"][0] == {"op": "visit", "node": "A", "line": 5}
    assert blueprint["steps"][1] == {"op": "edge", "from": "A", "to": "B", "weight": 2}


def test_step_and_critical_line_counts_are_capped():
    blueprint = parse_blueprint_response(_raw(
        criticalLines=[{"line": 1, "purpose": "x"}] * 20,
        simulationModel={"type": "array", "initialState": [0, 1]},
        steps=[{"op": "compare", "indices": [0, 1]}] * 500,
    ), line_count=100)
    assert len(blueprint["criticalLines"]) == 8
    assert len(blueprint["steps"]) == 300

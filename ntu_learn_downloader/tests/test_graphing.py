import json

from ntu_learn_downloader.graphing import parse_graph_response


def test_a_clean_single_variable_function_survives():
    spec = parse_graph_response(json.dumps({
        "functions": [{"expr": "x^2+3*x-1", "vars": ["x"]}],
        "domain": [-5, 5], "range": [-10, 10],
    }))
    assert spec == {"functions": [{"expr": "x^2+3*x-1", "vars": ["x"]}],
                    "domain": [-5.0, 5.0], "range": [-10.0, 10.0]}


def test_a_surface_with_named_math_functions_survives():
    spec = parse_graph_response(json.dumps({
        "functions": [{"expr": "sin(x)*cos(y)", "vars": ["x", "y"]}],
    }))
    assert spec["functions"] == [{"expr": "sin(x)*cos(y)", "vars": ["x", "y"]}]
    # No domain/range stated - falls back to the default window.
    assert spec["domain"] == [-8.0, 8.0]
    assert spec["range"] == [-8.0, 8.0]


def test_multiple_functions_up_to_the_cap_survive():
    spec = parse_graph_response(json.dumps({
        "functions": [{"expr": "x", "vars": ["x"]}] * 6,
    }))
    assert len(spec["functions"]) == 4


def test_a_response_wrapped_in_a_markdown_fence_is_unwrapped():
    raw = "```json\n" + json.dumps({"functions": [{"expr": "x", "vars": ["x"]}]}) + "\n```"
    assert parse_graph_response(raw)["functions"] == [{"expr": "x", "vars": ["x"]}]


def test_prose_around_a_json_object_is_salvaged():
    raw = 'Sure, here it is: {"functions": [{"expr": "x", "vars": ["x"]}]} - hope that helps!'
    assert parse_graph_response(raw)["functions"] == [{"expr": "x", "vars": ["x"]}]


def test_unparseable_text_yields_no_functions_not_an_error():
    spec = parse_graph_response("I don't see an explicit function on this page.")
    assert spec == {"functions": [], "domain": [-8.0, 8.0], "range": [-8.0, 8.0]}


def test_an_empty_functions_list_is_reported_as_is():
    assert parse_graph_response('{"functions": []}')["functions"] == []


# ── the allowlist is the security boundary: this must hold ──────────────────

def test_a_geogebra_command_name_in_the_expression_is_rejected():
    spec = parse_graph_response(json.dumps({
        "functions": [{"expr": "Execute({SetValue(a,1)})", "vars": ["x"]}],
    }))
    assert spec["functions"] == []


def test_a_semicolon_command_separator_is_rejected_even_without_letters():
    spec = parse_graph_response(json.dumps({
        "functions": [{"expr": "1;2", "vars": ["x"]}],
    }))
    assert spec["functions"] == []


def test_an_unlisted_identifier_is_rejected():
    spec = parse_graph_response(json.dumps({
        "functions": [{"expr": "z+1", "vars": ["x"]}],
    }))
    assert spec["functions"] == []


def test_a_third_variable_is_rejected():
    spec = parse_graph_response(json.dumps({
        "functions": [{"expr": "x+y+z", "vars": ["x", "y", "z"]}],
    }))
    assert spec["functions"] == []


def test_an_overlong_expression_is_rejected():
    spec = parse_graph_response(json.dumps({
        "functions": [{"expr": "x+" * 150 + "1", "vars": ["x"]}],
    }))
    assert spec["functions"] == []


def test_a_non_dict_function_entry_is_skipped_not_fatal():
    spec = parse_graph_response(json.dumps({
        "functions": ["x^2", {"expr": "x", "vars": ["x"]}],
    }))
    assert spec["functions"] == [{"expr": "x", "vars": ["x"]}]


def test_domain_bounds_are_clamped_to_the_limit():
    spec = parse_graph_response(json.dumps({
        "functions": [{"expr": "x", "vars": ["x"]}],
        "domain": [-99999, 99999],
    }))
    assert spec["domain"] == [-200.0, 200.0]


def test_an_inverted_or_degenerate_bound_falls_back_to_default():
    spec = parse_graph_response(json.dumps({
        "functions": [{"expr": "x", "vars": ["x"]}],
        "range": [5, 5],
    }))
    assert spec["range"] == [-8.0, 8.0]


def test_garbage_input_never_raises():
    for raw in (None, "", "{", "[]", "null", "12345", '{"functions": "not a list"}'):
        parse_graph_response(raw)

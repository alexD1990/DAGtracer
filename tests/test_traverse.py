import pytest

from dgt.errors import DgtError
from dgt.findings import Cause, Status
from dgt.graph import DagGraph, Direction
from dgt.traverse import trace_column

from factory import make_project

P = "testproject"
UP = [Direction.UP]


def trace(project, start, column, full_trace=False):
    return trace_column(
        project, DagGraph(project.manifest), project.find_node(start), column, UP, full_trace
    )


def found(report):
    return [(f.cause.value, f.node.display_name, f.depth) for t in report.traces for f in t.findings]


def finding_at(report, name):
    (finding,) = [f for t in report.traces for f in t.findings if f.node.display_name == name]
    return finding


def parents(report, name):
    return {p.node.display_name: p.status.value for p in finding_at(report, name).parents}


def candidates(report, name):
    finding = finding_at(report, name)
    return [n.display_name for n in finding.candidates] if finding.uncertain else None


def levels(report):
    return {v.node.display_name: (v.depth, v.status.value) for v in report.traces[0].visits}


# Scenarios in testproject


def test_column_appears_in_intermediate(project):
    report = trace(project, "fct_orders", "order_size")
    assert report.start_status is Status.PRESENT
    assert found(report) == [("column_appears", "int_orders_enriched", -1)]
    assert parents(report, "int_orders_enriched") == {"stg_orders": "absent"}
    assert levels(report)["int_order_payments"] == (-1, "unknown")


def test_column_dropped_in_intermediate(project):
    report = trace(project, "int_customers_slim", "last_name")
    assert report.start_status is Status.ABSENT
    assert found(report) == [("column_dropped", "int_customers_slim", 0)]
    assert parents(report, "int_customers_slim") == {"stg_customers": "present"}
    assert levels(report) == {"stg_customers": (-1, "present")}


def test_diamond_reports_only_origin(project):
    """Absent sibling branches (int_customers_slim, fct_orders) are not findings."""
    report = trace(project, "dim_customers", "last_name")
    assert found(report) == [("present_at_source", "raw_customers", -3)]
    assert levels(report) == {
        "fct_orders": (-1, "absent"),
        "int_customers_named": (-1, "present"),
        "int_customers_slim": (-1, "absent"),
        "stg_customers": (-2, "present"),
        "raw_customers": (-3, "present"),
    }


def test_stop_does_not_expand_changed_nodes(project):
    report = trace(project, "dim_customers", "last_name")
    stopped = {v.node.name for v in report.traces[0].visits if v.stopped}
    assert stopped == {"fct_orders", "int_customers_slim"}


def test_full_trace_judges_every_node(project):
    report = trace(project, "dim_customers", "last_name", full_trace=True)
    assert set(levels(report)) == {
        "fct_orders", "int_customers_named", "int_customers_slim", "int_order_payments",
        "int_orders_enriched", "int_orders_valid", "stg_customers", "stg_orders",
        "stg_payments", "raw_customers", "raw_orders", "raw.payments",
    }
    assert not any(v.stopped for v in report.traces[0].visits)
    assert found(report) == [
        ("column_dropped", "int_customers_slim", -1),
        ("present_at_source", "raw_customers", -3),
    ]


def test_column_case_insensitive(project):
    assert found(trace(project, "dim_customers", "LAST_NAME")) == found(
        trace(project, "dim_customers", "last_name")
    )


def test_column_nowhere_traverses_everything_without_findings(project):
    report = trace(project, "stg_payments", "nope")
    assert report.start_status is Status.ABSENT
    assert found(report) == []
    assert levels(report) == {"raw.payments": (-1, "absent")}


def test_ephemeral_parent_is_transparent(project):
    report = trace(project, "fct_orders", "total_paid")
    assert found(report) == [("column_appears", "fct_orders", 0)]
    assert candidates(report, "fct_orders") == ["fct_orders", "int_order_payments"]
    (via_ephemeral,) = [p for p in finding_at(report, "fct_orders").parents if p.via]
    assert via_ephemeral.node.display_name == "stg_payments"
    assert [n.display_name for n in via_ephemeral.via] == ["int_order_payments"]


def test_source_start_is_origin(project):
    report = trace(project, "raw.payments", "method")
    assert report.traces[0].visits == []
    assert found(report) == [("present_at_source", "raw.payments", 0)]


def test_unknown_start_is_error(project):
    with pytest.raises(DgtError, match="ephemeral"):
        trace(project, "int_order_payments", "total_paid")


def test_fresh_project_has_no_warnings(project):
    assert trace(project, "dim_customers", "last_name").warnings == []


# Synthetic graphs


def test_not_in_catalog_is_traversed_through_with_warning():
    project = make_project([("a", "b"), ("b", "c")], {"a": ["x"], "b": None, "c": ["x"]})
    report = trace(project, "c", "x")
    assert levels(report) == {"b": (-1, "unknown"), "a": (-2, "present")}
    assert found(report) == [("column_appears", "a", -2)]
    assert candidates(report, "a") is None
    assert report.warnings == [
        "model.p.b is not in catalog.json; column status unknown, traversed through"
    ]


def test_appears_behind_unknown_node_is_uncertain():
    project = make_project([("a", "b"), ("b", "c")], {"a": [], "b": None, "c": ["x"]})
    report = trace(project, "c", "x")
    assert found(report) == [("column_appears", "c", 0)]
    assert candidates(report, "c") == ["c", "b"]
    assert parents(report, "c") == {"a": "absent"}


def test_dropped_through_ephemeral_is_uncertain():
    project = make_project(
        [("a", "b"), ("b", "c")], {"a": ["x"], "b": None, "c": []}, ephemeral={"b"}
    )
    report = trace(project, "c", "x")
    assert found(report) == [("column_dropped", "c", 0)]
    assert candidates(report, "c") == ["c", "b"]


def test_dropped_with_direct_present_parent_is_certain():
    project = make_project(
        [("a", "b"), ("b", "c"), ("a", "c")], {"a": ["x"], "b": None, "c": []}, ephemeral={"b"}
    )
    report = trace(project, "c", "x")
    assert candidates(report, "c") is None
    assert finding_at(report, "c").parents[0].via == ()


def test_unknown_parent_without_parents_makes_appearance_uncertain():
    project = make_project([("b", "c")], {"b": None, "c": ["x"]})
    report = trace(project, "c", "x")
    assert candidates(report, "c") == ["c", "b"]
    assert parents(report, "c") == {"b": "unknown"}


def test_diamond_judges_each_branch():
    # a -> b -> d, a -> c -> d; x first appears in both b and c.
    project = make_project(
        [("a", "b"), ("a", "c"), ("b", "d"), ("c", "d")],
        {"a": [], "b": ["x"], "c": ["x"], "d": ["x"]},
    )
    report = trace(project, "d", "x")
    assert found(report) == [("column_appears", "b", -1), ("column_appears", "c", -1)]
    assert [v.node.name for v in report.traces[0].visits].count("a") == 1


def test_absent_parent_beside_present_parent_is_not_a_finding():
    project = make_project([("a", "c"), ("b", "c")], {"a": ["x"], "b": [], "c": ["x"]})
    report = trace(project, "c", "x")
    assert found(report) == [("column_appears", "a", -1)]
    assert levels(report)["b"] == (-1, "absent")


def test_model_without_parents_is_where_column_appears():
    project = make_project([], {"a": ["x"]})
    assert found(trace(project, "a", "x")) == [("column_appears", "a", 0)]


def test_full_trace_reports_reappearing_column():
    project = make_project([("a", "b"), ("b", "c")], {"a": ["x"], "b": [], "c": ["x"]})
    assert found(trace(project, "c", "x", full_trace=True)) == [
        ("column_appears", "c", 0),
        ("column_dropped", "b", -1),
        ("column_appears", "a", -2),
    ]


def test_seed_terminal_upstream():
    project = make_project([("seed:s", "a")], {"seed:s": ["x"], "a": ["x"]})
    report = trace(project, "a", "x")
    assert levels(report) == {"s": (-1, "present")}
    assert found(report) == [("present_at_source", "s", -1)]


def test_stale_catalog_warning():
    project = make_project(
        [], {"a": ["x"]}, generated_at=("2024-02-01T00:00:00Z", "2024-01-01T00:00:00Z")
    )
    (warning,) = trace(project, "a", "x").warnings
    assert warning.startswith("catalog.json (2024-01-01 00:00:00) is older than manifest.json")

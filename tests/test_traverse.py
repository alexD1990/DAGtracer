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


def edges(report):
    return sorted(
        (f.cause.value, f.upstream.display_name, f.downstream.display_name, f.depth)
        for t in report.traces
        for f in t.findings
    )


def levels(report):
    return {v.node.display_name: (v.depth, v.status.value) for v in report.traces[0].visits}


# Scenarios in testproject


def test_column_appears_in_intermediate(project):
    report = trace(project, "fct_orders", "order_size")
    assert report.start_status is Status.PRESENT
    assert edges(report) == [
        ("column_appears", "int_orders_valid", "fct_orders", -1),
        ("column_appears", "stg_orders", "int_orders_enriched", -2),
        ("column_appears", "stg_payments", "int_order_payments", -2),
    ]
    assert levels(report)["int_order_payments"] == (-1, "unknown")


def test_column_dropped_in_intermediate(project):
    report = trace(project, "int_customers_slim", "last_name")
    assert report.start_status is Status.ABSENT
    assert edges(report) == [("column_dropped", "stg_customers", "int_customers_slim", -1)]
    assert levels(report) == {"stg_customers": (-1, "present")}


def test_diamond_one_branch_loses_column(project):
    report = trace(project, "dim_customers", "last_name")
    assert edges(report) == [
        ("column_appears", "fct_orders", "dim_customers", -1),
        ("column_appears", "int_customers_slim", "dim_customers", -1),
    ]
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


def test_full_trace_maps_everything_reachable(project):
    report = trace(project, "dim_customers", "last_name", full_trace=True)
    assert set(levels(report)) == {
        "fct_orders", "int_customers_named", "int_customers_slim", "int_order_payments",
        "int_orders_enriched", "int_orders_valid", "stg_customers", "stg_orders",
        "stg_payments", "raw_customers", "raw_orders", "raw.payments",
    }
    assert not any(v.stopped for v in report.traces[0].visits)
    assert edges(report) == [
        ("column_appears", "fct_orders", "dim_customers", -1),
        ("column_appears", "int_customers_slim", "dim_customers", -1),
        ("column_dropped", "stg_customers", "int_customers_slim", -2),
    ]


def test_column_case_insensitive(project):
    assert edges(trace(project, "dim_customers", "LAST_NAME")) == edges(
        trace(project, "dim_customers", "last_name")
    )


def test_column_nowhere_traverses_everything_without_findings(project):
    report = trace(project, "stg_payments", "nope")
    assert report.start_status is Status.ABSENT
    assert edges(report) == []
    assert levels(report) == {"raw.payments": (-1, "absent")}


def test_source_start(project):
    report = trace(project, "raw.payments", "method")
    assert report.traces[0].visits == []


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
    assert report.warnings == [
        "model.p.b is not in catalog.json; column status unknown, traversed through"
    ]


def test_change_behind_unknown_node_reports_edge_from_unknown():
    project = make_project([("a", "b"), ("b", "c")], {"a": [], "b": None, "c": ["x"]})
    assert edges(trace(project, "c", "x")) == [("column_appears", "a", "b", -2)]


def test_diamond_reports_each_changing_edge():
    # a -> b -> d, a -> c -> d; a lacks x, so both edges into it change.
    project = make_project(
        [("a", "b"), ("a", "c"), ("b", "d"), ("c", "d")],
        {"a": [], "b": ["x"], "c": ["x"], "d": ["x"]},
    )
    report = trace(project, "d", "x")
    assert edges(report) == [
        ("column_appears", "a", "b", -2),
        ("column_appears", "a", "c", -2),
    ]
    assert [v.node.name for v in report.traces[0].visits].count("a") == 1


def test_full_trace_reports_reappearing_column():
    project = make_project([("a", "b"), ("b", "c")], {"a": ["x"], "b": [], "c": ["x"]})
    assert edges(trace(project, "c", "x", full_trace=True)) == [
        ("column_appears", "b", "c", -1),
        ("column_dropped", "a", "b", -2),
    ]


def test_seed_terminal_upstream():
    project = make_project([("seed:s", "a")], {"seed:s": ["x"], "a": ["x"]})
    assert levels(trace(project, "a", "x")) == {"s": (-1, "present")}


def test_stale_catalog_warning():
    project = make_project(
        [], {"a": ["x"]}, generated_at=("2024-02-01T00:00:00Z", "2024-01-01T00:00:00Z")
    )
    (warning,) = trace(project, "a", "x").warnings
    assert warning.startswith("catalog.json (2024-01-01 00:00:00) is older than manifest.json")

import pytest

from dgt.errors import DgtError
from dgt.findings import Cause, Status
from dgt.graph import DagGraph, Direction
from dgt.probes import DbtShow, ValueProbe
from dgt.traverse import trace_column

from factory import FakeShow, make_project

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
    absent_sources = {"raw_orders", "raw.payments"}
    assert set(levels(report)) == {
        "fct_orders", "int_customers_named", "int_customers_slim", "int_order_payments",
        "int_orders_enriched", "int_orders_valid", "stg_customers", "stg_orders",
        "stg_payments", "raw_customers", "raw_orders", "raw.payments",
    }
    assert not any(v.stopped for v in report.traces[0].visits)
    assert found(report) == [
        ("column_absent_at_source", "raw.payments", -4),
        ("column_absent_at_source", "raw_orders", -4),
        ("present_at_source", "raw_customers", -3),
        ("column_dropped", "int_customers_slim", -1),
    ]
    assert absent_sources == {f[1] for f in found(report) if f[0] == "column_absent_at_source"}


def test_column_case_insensitive(project):
    assert found(trace(project, "dim_customers", "LAST_NAME")) == found(
        trace(project, "dim_customers", "last_name")
    )


def test_column_nowhere_reports_absent_at_every_reached_source(project):
    report = trace(project, "dim_customers", "nope")
    assert report.start_status is Status.ABSENT
    assert found(report) == [
        ("column_absent_at_source", "raw.payments", -4),
        ("column_absent_at_source", "raw_orders", -4),
        ("column_absent_at_source", "raw_customers", -3),
    ]
    assert not any(v.stopped for v in report.traces[0].visits)


def test_absent_source_on_present_branch_is_not_reported(project):
    # raw_orders lacks last_name but is never reached along an absent branch.
    assert "raw_orders" not in {f[1] for f in found(trace(project, "dim_customers", "last_name"))}


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


def test_ephemeral_start_suggests_neighbors(project):
    with pytest.raises(DgtError) as exc:
        trace(project, "int_order_payments", "total_paid")
    assert str(exc.value) == (
        "status of column 'total_paid' in model.testproject.int_order_payments is unknown: "
        "ephemeral models have no relation in catalog.json; "
        "start from a parent (stg_payments) or a child (fct_orders) instead"
    )


def test_start_missing_from_catalog_suggests_docs_generate():
    project = make_project([], {"a": None})
    with pytest.raises(DgtError, match=r"not in catalog.json; run `dbt docs generate`"):
        trace(project, "a", "x")


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
        ("column_appears", "a", -2),
        ("column_dropped", "b", -1),
        ("column_appears", "c", 0),
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


# Value mode


def vtrace(project, start, column, values, counts, full_trace=False, fail=()):
    show = FakeShow(counts, fail)
    report = trace_column(
        project, DagGraph(project.manifest), project.find_node(start), column, UP, full_trace,
        value_probe=ValueProbe(values, show),
    )
    return report, show


def vfound(report):
    return [
        (f.cause.value, f.node.display_name, f.depth, list(f.values))
        for t in report.traces
        for f in t.findings
    ]


def test_value_dropped_while_column_kept():
    project = make_project([("seed:s", "b")], {"seed:s": ["x"], "b": ["x"]})
    report, _ = vtrace(project, "b", "x", ["k", "m"], {"s": {"k": 1, "m": 1}, "b": {"k": 1}})
    assert report.start_values == {"k": Status.PRESENT, "m": Status.ABSENT}
    assert vfound(report) == [
        ("value_present_at_source", "s", -1, ["k"]),
        ("value_dropped", "b", 0, ["m"]),
    ]


def test_value_absent_at_source_on_absent_branch():
    project = make_project([("seed:s", "b")], {"seed:s": ["x"], "b": ["x"]})
    report, _ = vtrace(project, "b", "x", ["k", "m"], {"s": {"k": 1}, "b": {"k": 1}})
    assert vfound(report) == [
        ("value_absent_at_source", "s", -1, ["m"]),
        ("value_present_at_source", "s", -1, ["k"]),
    ]


def test_value_appears():
    project = make_project([("seed:s", "b")], {"seed:s": ["x"], "b": ["x"]})
    report, _ = vtrace(project, "b", "x", ["k", "m"], {"s": {"k": 1}, "b": {"k": 1, "m": 1}})
    assert vfound(report) == [
        ("value_present_at_source", "s", -1, ["k"]),
        ("value_appears", "b", 0, ["m"]),
    ]


def test_column_level_takes_precedence_and_skips_value_query():
    project = make_project([("seed:s", "a")], {"seed:s": ["x"], "a": []})
    report, show = vtrace(project, "a", "x", ["k", "m"], {"s": {"k": 1}})
    assert vfound(report) == [("column_dropped", "a", 0, ["k"])]
    assert report.traces[0].findings[0].values == {"k": Status.ABSENT}
    assert show.queries == ["s"]


def test_column_change_stops_branch_even_if_value_status_matches():
    # k is absent in b and (implicitly) in a, but a lacks the column.
    project = make_project([("a", "b")], {"a": [], "b": ["x"]})
    report, _ = vtrace(project, "b", "x", ["k"], {})
    (visit,) = report.traces[0].visits
    assert visit.stopped
    # a is not judged; b is, at column level, listing the tracked values.
    assert vfound(report) == [("column_appears", "b", 0, ["k"])]
    assert report.traces[0].findings[0].values == {"k": Status.ABSENT}


def test_branch_continues_while_one_value_unchanged():
    project = make_project([("s", "a"), ("a", "b")], {"s": ["x"], "a": ["x"], "b": ["x"]})
    counts = {"s": {"k": 1, "m": 1}, "a": {"k": 1}}
    report, _ = vtrace(project, "b", "x", ["k", "m"], counts)
    stopped = {v.node.name: v.stopped for v in report.traces[0].visits}
    assert stopped == {"a": False, "s": True}
    assert vfound(report) == [
        ("value_dropped", "a", -1, ["m"]),
        ("value_dropped", "b", 0, ["k"]),
    ]


def test_unknown_node_transparent_for_values():
    project = make_project(
        [("seed:s", "e"), ("e", "b")], {"seed:s": ["x"], "e": None, "b": ["x"]}, ephemeral={"e"}
    )
    report, _ = vtrace(project, "b", "x", ["k"], {"s": {"k": 1}})
    assert vfound(report) == [("value_dropped", "b", 0, ["k"])]
    (finding,) = report.traces[0].findings
    assert [n.name for n in finding.candidates] == ["b", "e"]


def test_node_reached_again_with_new_live_values_is_expanded():
    # d <- a <- c and d <- b <- b2 <- c: c is first reached with only k live
    # and later with m, which must propagate to the seed.
    project = make_project(
        [("seed:s", "c"), ("c", "a"), ("c", "b2"), ("b2", "b"), ("a", "d"), ("b", "d")],
        {name: ["x"] for name in ["seed:s", "c", "a", "b2", "b", "d"]},
    )
    both = {"k": 1, "m": 1}
    counts = {"s": both, "c": both, "d": both, "a": {"k": 1}, "b": {"m": 1}, "b2": {"m": 1}}
    report, _ = vtrace(project, "d", "x", ["k", "m"], counts)
    assert ("value_present_at_source", "s", -3, ["k", "m"]) in vfound(report)


def test_full_trace_judges_all_values():
    project = make_project([("seed:s", "b")], {"seed:s": ["x"], "b": ["x"]})
    counts = {"s": {"k": 1, "m": 1}, "b": {"k": 1}}
    report, _ = vtrace(project, "b", "x", ["k", "m"], counts, full_trace=True)
    assert vfound(report) == [
        ("value_present_at_source", "s", -1, ["k", "m"]),
        ("value_dropped", "b", 0, ["m"]),
    ]


def test_values_against_real_dbt(project, built_project_dir, dbt_executable):
    """int_orders_valid filters out status = 'returned'."""
    show = DbtShow(dbt_executable, built_project_dir, built_project_dir)
    report = trace_column(
        project, DagGraph(project.manifest), project.find_node("fct_orders"), "status", UP,
        value_probe=ValueProbe(["completed", "returned"], show),
    )
    assert report.column_type == "VARCHAR"
    assert vfound(report) == [
        ("value_present_at_source", "raw_orders", -3, ["completed"]),
        ("value_dropped", "int_orders_valid", -1, ["returned"]),
    ]
    assert parents(report, "int_orders_valid") == {"stg_orders": "present"}


def test_string_value_matches_integer_column(project, built_project_dir, dbt_executable):
    show = DbtShow(dbt_executable, built_project_dir, built_project_dir)
    report = trace_column(
        project, DagGraph(project.manifest), project.find_node("stg_orders"), "order_id", UP,
        value_probe=ValueProbe(["01", "7"], show),
    )
    assert report.column_type == "INTEGER"
    assert report.start_values == {"01": Status.PRESENT, "7": Status.ABSENT}


def test_first_value_query_failure_is_tool_error():
    project = make_project([("seed:s", "b")], {"seed:s": ["x"], "b": ["x"]})
    with pytest.raises(DgtError, match=r"first value query failed \(model.p.b\)"):
        vtrace(project, "b", "x", ["k"], {}, fail={"b"})


def test_later_query_failure_makes_node_unknown_and_transparent():
    project = make_project(
        [("seed:s", "a"), ("a", "b")], {"seed:s": ["x"], "a": ["x"], "b": ["x"]}
    )
    report, _ = vtrace(project, "b", "x", ["k"], {"s": {"k": 1}}, fail={"a"})
    a = next(v for v in report.traces[0].visits if v.node.name == "a")
    assert a.status is Status.UNKNOWN
    assert a.unknown_reason.value == "query_error"
    assert a.detail == (
        "dbt show failed (exit 1) ... Catalog Error: Table a does not exist"
    )
    assert vfound(report) == [("value_dropped", "b", 0, ["k"])]
    assert [n.name for n in report.traces[0].findings[0].candidates] == ["b", "a"]
    assert report.warnings == [
        "model.p.a: value query failed; status unknown, traversed through: "
        "dbt show failed (exit 1) ... Catalog Error: Table a does not exist"
    ]


def test_certain_and_uncertain_drops_at_one_node_are_separate_findings():
    # k reaches b directly from s; m only through the ephemeral e.
    project = make_project(
        [("seed:s", "b"), ("seed:t", "e"), ("e", "b")],
        {"seed:s": ["x"], "seed:t": ["x"], "e": None, "b": ["x"]},
        ephemeral={"e"},
    )
    report, _ = vtrace(project, "b", "x", ["k", "m"], {"s": {"k": 1}, "t": {"m": 1}})
    dropped = [f for f in report.traces[0].findings if f.cause.value == "value_dropped"]
    assert [(list(f.values), f.uncertain, [n.name for n in f.candidates]) for f in dropped] == [
        (["k"], False, []),
        (["m"], True, ["b", "e"]),
    ]


# Downstream and both

DOWN = [Direction.DOWN]
BOTH = [Direction.UP, Direction.DOWN]


def dtrace(project, start, column, directions=DOWN, full_trace=False, values=(), counts=None):
    value_probe = ValueProbe(values, FakeShow(counts or {})) if values else None
    return trace_column(
        project, DagGraph(project.manifest), project.find_node(start), column, directions,
        full_trace, value_probe=value_probe,
    )


def dfound(report):
    return [
        (f.direction.value, f.cause.value, f.node.display_name, f.depth, list(f.values))
        for t in report.traces
        for f in t.findings
    ]


def test_partial_spread_downstream(project):
    report = dtrace(project, "stg_orders", "status")
    assert dfound(report) == [
        ("down", "column_dropped", "int_orders_enriched", 1, []),
        ("down", "column_dropped", "dim_customers", 3, []),
    ]
    assert {v.node.name: (v.depth, v.stopped) for v in report.traces[0].visits} == {
        "int_orders_enriched": (1, True),
        "int_orders_valid": (1, False),
        "fct_orders": (2, False),
        "dim_customers": (3, True),
    }


def test_downstream_diamond(project):
    report = dtrace(project, "stg_customers", "last_name")
    assert dfound(report) == [
        ("down", "present_at_leaf", "customers_snapshot", 1, []),
        ("down", "column_dropped", "int_customers_slim", 1, []),
        ("down", "present_at_leaf", "dim_customers", 2, []),
    ]


def test_downstream_appears_considers_other_parents(project):
    # order_size appears in int_orders_enriched; fct_orders gets it from there,
    # not from int_orders_valid on the traversed path, so it is not a finding.
    report = dtrace(project, "stg_orders", "order_size")
    assert dfound(report) == [("down", "column_appears", "int_orders_enriched", 1, [])]
    assert {v.node.name: v.stopped for v in report.traces[0].visits}["fct_orders"]


def test_downstream_present_child_with_present_other_parent_is_not_appears(project):
    assert dfound(dtrace(project, "int_customers_named", "country")) == []


def test_both_is_two_traversals_in_one_report(project):
    report = dtrace(project, "dim_customers", "last_name", BOTH)
    assert [t.direction for t in report.traces] == BOTH
    assert dfound(report) == [
        ("up", "present_at_source", "raw_customers", -3, []),
        ("down", "present_at_leaf", "dim_customers", 0, []),
    ]


def test_partial_spread_downstream_values_against_real_dbt(
    project, built_project_dir, dbt_executable
):
    show = DbtShow(dbt_executable, built_project_dir, built_project_dir)
    report = trace_column(
        project, DagGraph(project.manifest), project.find_node("stg_orders"), "status", DOWN,
        value_probe=ValueProbe(["completed", "returned"], show),
    )
    assert dfound(report) == [
        ("down", "column_dropped", "int_orders_enriched", 1, ["completed", "returned"]),
        ("down", "value_dropped", "int_orders_valid", 1, ["returned"]),
        ("down", "column_dropped", "dim_customers", 3, ["completed"]),
    ]


def test_downstream_start_not_judged_against_its_parents():
    project = make_project([("a", "b")], {"a": [], "b": ["x"]})
    assert dfound(dtrace(project, "b", "x")) == [("down", "present_at_leaf", "b", 0, [])]
    assert dfound(dtrace(project, "b", "x", BOTH)) == [
        ("up", "column_appears", "b", 0, []),
        ("down", "present_at_leaf", "b", 0, []),
    ]


def test_downstream_drop_through_ephemeral_is_uncertain():
    project = make_project(
        [("s", "e"), ("e", "c")], {"s": ["x"], "e": None, "c": []}, ephemeral={"e"}
    )
    report = dtrace(project, "s", "x")
    assert dfound(report) == [("down", "column_dropped", "c", 2, [])]
    assert [n.name for n in report.traces[0].findings[0].candidates] == ["c", "e"]


def test_leaf_only_for_live_keys():
    # Column absent from the start: a leaf where it is present is where it
    # appears, not how far it got.
    project = make_project([("a", "b")], {"a": [], "b": ["x"]})
    assert dfound(dtrace(project, "a", "x")) == [("down", "column_appears", "b", 1, [])]


def test_downstream_values_leaf_and_drop():
    project = make_project([("a", "b"), ("a", "c")], {"a": ["x"], "b": ["x"], "c": ["x"]})
    counts = {"a": {"k": 1, "m": 1}, "b": {"k": 1, "m": 1}, "c": {"k": 1}}
    report = dtrace(project, "a", "x", values=["k", "m"], counts=counts)
    assert dfound(report) == [
        ("down", "value_present_at_leaf", "b", 1, ["k", "m"]),
        ("down", "value_dropped", "c", 1, ["m"]),
        ("down", "value_present_at_leaf", "c", 1, ["k"]),
    ]


def test_downstream_full_trace_continues_past_changes():
    project = make_project([("a", "b"), ("b", "c")], {"a": ["x"], "b": [], "c": ["x"]})
    report = dtrace(project, "a", "x", full_trace=True)
    assert dfound(report) == [
        ("down", "column_dropped", "b", 1, []),
        ("down", "column_appears", "c", 2, []),
        ("down", "present_at_leaf", "c", 2, []),
    ]


def test_column_nowhere_downstream_is_absent_at_leaf(project):
    report = dtrace(project, "stg_orders", "nope")
    assert dfound(report) == [("down", "column_absent_at_leaf", "dim_customers", 3, [])]
    assert report.warnings == [
        "column 'nope' is absent in stg_orders; downstream findings show where it appears "
        "or stays absent, not how far it spreads"
    ]


def test_value_absent_at_leaf():
    project = make_project([("a", "b")], {"a": ["x"], "b": ["x"]})
    counts = {"a": {"k": 1}, "b": {"k": 1}}
    report = dtrace(project, "a", "x", values=["k", "m"], counts=counts)
    assert dfound(report) == [
        ("down", "value_absent_at_leaf", "b", 1, ["m"]),
        ("down", "value_present_at_leaf", "b", 1, ["k"]),
    ]
    assert report.warnings == [
        "value(s) m absent in a; downstream findings show where it appears "
        "or stays absent, not how far it spreads"
    ]


def test_column_absent_at_leaf_lists_values():
    project = make_project([("a", "b")], {"a": [], "b": []})
    report = dtrace(project, "a", "x", values=["k"], counts={})
    assert dfound(report) == [("down", "column_absent_at_leaf", "b", 1, ["k"])]


def test_no_absent_start_warning_upstream():
    project = make_project([("a", "b")], {"a": [], "b": []})
    assert dtrace(project, "b", "x", UP).warnings == []


def test_column_absent_at_source_lists_values():
    project = make_project([("seed:s", "b")], {"seed:s": [], "b": []})
    report, _ = vtrace(project, "b", "x", ["k", "m"], {})
    assert vfound(report) == [("column_absent_at_source", "s", -1, ["k", "m"])]


def test_column_dropped_without_lost_values_lists_none():
    project = make_project([("seed:s", "a")], {"seed:s": ["x"], "a": []})
    report, _ = vtrace(project, "a", "x", ["k"], {})
    assert vfound(report) == [("column_dropped", "a", 0, [])]

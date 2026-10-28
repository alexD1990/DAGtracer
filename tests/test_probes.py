import shutil
import sys

import pytest

from dgt.errors import DgtError
from dgt.findings import Status, UnknownReason
from dgt.probes import (
    ColumnProbe,
    DbtShow,
    Prober,
    ValueProbe,
    build_value_sql,
    find_dbt_executable,
    parse_counts,
    quote_ident,
    sql_literal,
)

from factory import FakeShow, make_project


def _probe(project, column, name):
    return ColumnProbe(project, column).probe(project.node(f"model.p.{name}"))


def test_present_case_insensitive():
    project = make_project([], {"a": ["Customer_ID"]})
    assert _probe(project, "CUSTOMER_id", "a").status is Status.PRESENT


def test_column_missing_is_absent():
    project = make_project([], {"a": ["x"]})
    assert _probe(project, "y", "a").status is Status.ABSENT


def test_empty_column_set_is_absent():
    project = make_project([], {"a": []})
    assert _probe(project, "y", "a").status is Status.ABSENT


def test_not_in_catalog_is_unknown():
    project = make_project([], {"a": None})
    result = _probe(project, "x", "a")
    assert result.status is Status.UNKNOWN
    assert result.unknown_reason is UnknownReason.NOT_IN_CATALOG


def test_ephemeral_is_unknown_even_if_in_catalog():
    project = make_project([], {"a": ["x"]}, ephemeral={"a"})
    result = _probe(project, "x", "a")
    assert result.status is Status.UNKNOWN
    assert result.unknown_reason is UnknownReason.EPHEMERAL


def test_source_columns_read_from_sources_section():
    project = make_project([], {"source:raw.t": ["x"]})
    node = project.node("source.p.raw.t")
    assert ColumnProbe(project, "x").probe(node).status is Status.PRESENT


def test_results_cached():
    project = make_project([], {"a": ["x"]})
    probe = ColumnProbe(project, "x")
    node = project.node("model.p.a")
    probe.probe(node)
    project.catalog["nodes"].clear()
    assert probe.probe(node).status is Status.PRESENT


# Value probing


def test_quote_ident_uses_adapter_quote():
    assert quote_ident("Order_ID") == '{{ adapter.quote("Order_ID") }}'


@pytest.mark.parametrize("name", ["1col", "a-b", "a b", 'x")}}', ""])
def test_quote_ident_rejects_unsafe_names(name):
    with pytest.raises(DgtError, match="invalid column name"):
        quote_ident(name)


@pytest.mark.parametrize("value", ["completed", "0222", "a b", "2024-01-05", "x:y+z/w.v_u"])
def test_sql_literal_accepts_whitelist(value):
    assert sql_literal(value) == f"'{value}'"


@pytest.mark.parametrize("value", ["it's", "a;b", "", "x" * 257, "æ", 'a"b', "a\nb"])
def test_sql_literal_rejects_outside_whitelist(value):
    with pytest.raises(DgtError, match="whitelist"):
        sql_literal(value)


def test_sql_literal_rejects_non_string():
    with pytest.raises(DgtError, match="must be a string"):
        sql_literal(222)


def test_build_value_sql_counts_every_value_in_one_query():
    sql = build_value_sql('"db"."main"."t"', "status", ["a", "b"])
    col = '{{ adapter.quote("status") }}'
    assert sql == (
        f"select count(case when {col} = 'a' then 1 end) as v0, "
        f"count(case when {col} = 'b' then 1 end) as v1 "
        f"from \"db\".\"main\".\"t\" where {col} in ('a', 'b')"
    )


def test_parse_counts_multiline_json_after_noise():
    stdout = 'log line {not json\n{\n  "show": [\n    {\n      "v0": 3,\n      "v1": 0\n    }\n  ]\n}\n'
    assert parse_counts(stdout, 2) == [3, 0]


def test_parse_counts_case_insensitive_aliases_and_data_key():
    assert parse_counts('{"data": [{"V1": "2", "V0": 5}]}', 2) == [5, 2]


def test_parse_counts_skips_objects_without_rows():
    assert parse_counts('{"node": "x"} {"show": [{"v0": 1}]}', 1) == [1]


def test_parse_counts_empty_rows_are_zero():
    assert parse_counts('{"show": []}', 2) == [0, 0]


def test_parse_counts_missing_alias_is_error():
    with pytest.raises(DgtError, match="v1"):
        parse_counts('{"show": [{"v0": 1}]}', 2)


def test_parse_counts_without_json_is_error():
    with pytest.raises(DgtError, match="no JSON result"):
        parse_counts("Encountered an error", 1)


def test_find_dbt_executable_prefers_override_then_venv(tmp_path, monkeypatch):
    assert find_dbt_executable("/opt/dbt") == "/opt/dbt"
    python = tmp_path / "bin" / "python"
    python.parent.mkdir()
    (tmp_path / "bin" / "dbt").write_text("")
    monkeypatch.setattr(sys, "executable", str(python))
    assert find_dbt_executable() == str(tmp_path / "bin" / "dbt")


def test_find_dbt_executable_falls_back_to_path(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "executable", str(tmp_path / "python"))
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/dbt")
    assert find_dbt_executable() == "/usr/bin/dbt"
    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(DgtError, match="not found"):
        find_dbt_executable()


def test_dbt_show_args_do_not_write_artifacts(tmp_path):
    show = DbtShow("dbt", tmp_path, tmp_path / "profiles", "ci")
    assert show.args("select 1") == [
        "dbt", "--no-write-json", "show", "--inline", "select 1", "--output", "json",
        "--quiet", "--project-dir", str(tmp_path), "--profiles-dir", str(tmp_path / "profiles"),
        "--target", "ci",
    ]


def test_dbt_show_missing_executable_is_tool_error(tmp_path):
    with pytest.raises(DgtError, match="cannot run dbt"):
        DbtShow(str(tmp_path / "no-dbt"), tmp_path)("select 1")


def test_value_probe_uses_catalog_column_casing():
    project = make_project([], {"a": ["Status"]})
    show = FakeShow({"a": {"x": 1}})
    prober = Prober(ColumnProbe(project, "STATUS"), ValueProbe(["x", "y"], show))
    state = prober.state(project.node("model.p.a"))
    assert state.values == {"x": Status.PRESENT, "y": Status.ABSENT}
    assert state.column.column_name == "Status"
    assert state.column.column_type == "TEXT"


def test_values_not_queried_without_column():
    project = make_project([], {"a": [], "b": None})
    show = FakeShow({})
    prober = Prober(ColumnProbe(project, "x"), ValueProbe(["k"], show))
    assert prober.state(project.node("model.p.a")).values == {"k": Status.ABSENT}
    assert prober.state(project.node("model.p.b")).values == {"k": Status.UNKNOWN}
    assert show.queries == []


def test_node_state_cached():
    project = make_project([], {"a": ["x"]})
    show = FakeShow({"a": {"k": 1}})
    prober = Prober(ColumnProbe(project, "x"), ValueProbe(["k"], show))
    node = project.node("model.p.a")
    prober.state(node)
    prober.state(node)
    assert show.queries == ["a"]

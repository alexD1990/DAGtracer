from dgt.findings import Status, UnknownReason
from dgt.probes import ColumnProbe

from factory import make_project


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

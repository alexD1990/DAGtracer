import pytest

from dgt.graph import DagGraph, Direction

P = "testproject"


@pytest.fixture(scope="module")
def graph(project):
    return DagGraph(project.manifest)


def test_upstream_parents(graph):
    assert graph.neighbors(f"model.{P}.dim_customers", Direction.UP) == [
        f"model.{P}.fct_orders",
        f"model.{P}.stg_customers",
    ]


def test_upstream_through_ephemeral(graph):
    assert graph.neighbors(f"model.{P}.fct_orders", Direction.UP) == [
        f"model.{P}.int_order_payments",
        f"model.{P}.stg_orders",
    ]
    assert graph.neighbors(f"model.{P}.int_order_payments", Direction.UP) == [
        f"model.{P}.stg_payments"
    ]


def test_upstream_reaches_source_and_seed(graph):
    assert graph.neighbors(f"model.{P}.stg_payments", Direction.UP) == [
        f"source.{P}.raw.payments"
    ]
    assert graph.neighbors(f"model.{P}.stg_orders", Direction.UP) == [f"seed.{P}.raw_orders"]


@pytest.mark.parametrize("uid", [f"seed.{P}.raw_orders", f"source.{P}.raw.payments"])
def test_seed_and_source_terminal_upstream(graph, uid):
    assert graph.neighbors(uid, Direction.UP) == []


def test_downstream_includes_snapshot(graph):
    assert graph.neighbors(f"model.{P}.stg_customers", Direction.DOWN) == [
        f"model.{P}.dim_customers",
        f"snapshot.{P}.customers_snapshot",
    ]


def test_downstream_excludes_tests_and_exposures(graph):
    assert graph.neighbors(f"model.{P}.fct_orders", Direction.DOWN) == [
        f"model.{P}.dim_customers"
    ]
    assert graph.neighbors(f"model.{P}.dim_customers", Direction.DOWN) == []


def test_downstream_from_seed_and_source(graph):
    assert graph.neighbors(f"seed.{P}.raw_orders", Direction.DOWN) == [f"model.{P}.stg_orders"]
    assert graph.neighbors(f"source.{P}.raw.payments", Direction.DOWN) == [
        f"model.{P}.stg_payments"
    ]


def test_unknown_node_has_no_neighbors(graph):
    assert graph.neighbors("model.x.nope", Direction.UP) == []
    assert graph.neighbors("model.x.nope", Direction.DOWN) == []


def test_semantic_models_filtered_downstream():
    manifest = {
        "nodes": {"model.a.m": {"resource_type": "model"}},
        "semantic_models": {"semantic_model.a.s": {"resource_type": "semantic_model"}},
        "parent_map": {},
        "child_map": {"model.a.m": ["semantic_model.a.s"]},
    }
    assert DagGraph(manifest).neighbors("model.a.m", Direction.DOWN) == []

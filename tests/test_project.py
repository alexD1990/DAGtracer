import json
import shutil

import pytest

from dgt.errors import AmbiguousNodeError, ArtifactError, NodeNotFoundError
from dgt.project import DbtProject, unresolved_dependency_nodes


def test_find_model_by_short_name(project):
    node = project.find_node("fct_orders")
    assert node.unique_id == "model.testproject.fct_orders"
    assert node.resource_type == "model"
    assert node.materialized == "table"


def test_find_by_unique_id(project):
    assert project.find_node("seed.testproject.raw_orders").name == "raw_orders"


def test_find_source_by_dotted_name(project):
    assert project.find_node("raw.payments").unique_id == "source.testproject.raw.payments"
    assert project.node("source.testproject.raw.payments").display_name == "raw.payments"


def test_bare_source_table_name_does_not_match(project):
    with pytest.raises(NodeNotFoundError, match="raw.payments"):
        project.find_node("payments")


def test_unknown_name_suggests_close_matches(project):
    with pytest.raises(NodeNotFoundError, match="fct_orders"):
        project.find_node("fct_order")


def test_tests_are_not_selectable(project):
    with pytest.raises(NodeNotFoundError):
        project.find_node("unique_fct_orders_order_id")


def test_relation_name_comes_from_manifest(project):
    assert project.node("model.testproject.fct_orders").relation_name == (
        '"testproject"."main"."fct_orders"'
    )
    assert project.node("source.testproject.raw.payments").relation_name == (
        '"testproject"."raw"."payments"'
    )


def test_ephemeral_model_has_no_relation(project):
    node = project.node("model.testproject.int_order_payments")
    assert node.is_ephemeral
    assert node.relation_name is None


def test_catalog_columns_lowercased(project):
    columns = project.catalog_columns("model.testproject.dim_customers")
    assert set(columns) == {"customer_id", "first_name", "last_name", "country", "order_count"}


def test_catalog_columns_for_source(project):
    columns = project.catalog_columns("source.testproject.raw.payments")
    assert set(columns) == {"id", "order_id", "method", "amount"}


def test_catalog_columns_none_for_ephemeral(project):
    assert project.catalog_columns("model.testproject.int_order_payments") is None


def test_fresh_catalog_is_not_stale(project):
    # With a full parse, docs generate stamps the catalog slightly before the
    # manifest it rewrites; the tolerance absorbs that.
    assert not project.catalog_is_stale()


def test_load_without_catalog_is_error(built_project_dir, tmp_path):
    copy = tmp_path / "p"
    shutil.copytree(built_project_dir / "target", copy / "target")
    shutil.copy(built_project_dir / "dbt_project.yml", copy)
    (copy / "target" / "catalog.json").unlink()
    with pytest.raises(ArtifactError, match="catalog.json not found.*dbt docs generate"):
        DbtProject.load(copy)


def test_load_without_manifest(tmp_path):
    (tmp_path / "dbt_project.yml").write_text("name: x\n")
    with pytest.raises(ArtifactError, match="dbt docs generate"):
        DbtProject.load(tmp_path)


def test_load_without_dbt_project(tmp_path):
    with pytest.raises(ArtifactError, match="dbt_project.yml"):
        DbtProject.load(tmp_path)


def test_custom_target_path(tmp_path):
    (tmp_path / "dbt_project.yml").write_text("name: x\ntarget-path: build\n")
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "manifest.json").write_text(json.dumps({"nodes": {}}))
    (tmp_path / "build" / "catalog.json").write_text(json.dumps({"nodes": {}}))
    assert DbtProject.load(tmp_path).catalog == {"nodes": {}}


EMPTY_CATALOG = {"metadata": {}, "nodes": {}, "sources": {}}


def _manifest(nodes=None, sources=None, generated_at="2024-01-01T00:00:00Z"):
    return {
        "metadata": {"generated_at": generated_at},
        "nodes": nodes or {},
        "sources": sources or {},
    }


def test_ambiguous_short_name_rejected(tmp_path):
    manifest = _manifest(
        nodes={
            "model.a.orders": {"name": "orders", "resource_type": "model"},
            "model.b.orders": {"name": "orders", "resource_type": "model"},
        }
    )
    project = DbtProject(tmp_path, manifest, EMPTY_CATALOG)
    with pytest.raises(AmbiguousNodeError, match="model.a.orders"):
        project.find_node("orders")
    assert project.find_node("model.b.orders").unique_id == "model.b.orders"


def test_short_name_prefers_model_over_source_table(tmp_path):
    manifest = _manifest(
        nodes={"model.a.payments": {"name": "payments", "resource_type": "model"}},
        sources={"source.a.raw.payments": {"name": "payments", "resource_type": "source"}},
    )
    project = DbtProject(tmp_path, manifest, EMPTY_CATALOG)
    assert project.find_node("payments").unique_id == "model.a.payments"
    assert project.find_node("raw.payments").unique_id == "source.a.raw.payments"


@pytest.mark.parametrize(
    "catalog_at, stale",
    [
        ("2024-01-01T00:00:00Z", True),
        ("2024-01-31T23:58:59Z", True),
        ("2024-01-31T23:59:00Z", False),
        ("2024-01-31T23:59:30Z", False),
        ("2024-02-01T00:00:05Z", False),
    ],
)
def test_stale_catalog_tolerates_one_minute(tmp_path, catalog_at, stale):
    manifest = _manifest(generated_at="2024-02-01T00:00:00Z")
    catalog = {"metadata": {"generated_at": catalog_at}, "nodes": {}, "sources": {}}
    assert DbtProject(tmp_path, manifest, catalog).catalog_is_stale() is stale


def test_catalog_section_matches_node_kind(tmp_path):
    catalog = {
        "metadata": {},
        "nodes": {"source.a.raw.t": {"columns": {"X": {}}}},
        "sources": {},
    }
    project = DbtProject(tmp_path, _manifest(), catalog)
    assert project.catalog_columns("source.a.raw.t") is None


def _write_artifacts(project_dir, manifest):
    (project_dir / "dbt_project.yml").write_text("name: x\n")
    (project_dir / "target").mkdir()
    (project_dir / "target" / "manifest.json").write_text(json.dumps(manifest))
    (project_dir / "target" / "catalog.json").write_text(json.dumps(EMPTY_CATALOG))


def _model(refs=(), sources=(), depends_on=()):
    return {
        "name": "m",
        "resource_type": "model",
        "refs": [{"name": r, "package": None, "version": None} for r in refs],
        "sources": [list(s) for s in sources],
        "depends_on": {"macros": [], "nodes": list(depends_on)},
    }


def test_manifest_with_unresolved_dependencies_is_rejected(tmp_path):
    manifest = _manifest(
        nodes={
            "model.a.ok": _model(refs=["base"], depends_on=["model.a.base"]),
            "model.a.base": _model(),
            "model.a.lost_ref": _model(refs=["base"]),
            "model.a.lost_source": _model(sources=[("raw", "t")]),
            "test.a.lost_test": _model(refs=["ok"]),
        }
    )
    _write_artifacts(tmp_path, manifest)
    with pytest.raises(ArtifactError) as exc:
        DbtProject.load(tmp_path)
    message = str(exc.value)
    assert "3 node(s)" in message
    assert "Run `dbt parse`" in message
    assert message.endswith("model.a.lost_ref\n  model.a.lost_source\n  test.a.lost_test")


def test_built_manifest_is_consistent(project):
    assert unresolved_dependency_nodes(project.manifest) == []

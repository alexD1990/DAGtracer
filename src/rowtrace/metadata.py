"""Step 2 — metadata layer (architectur.md §3 layer 1, §4).

Responsibilities:
  - read manifest.json and catalog.json
  - translate short names to node UIDs (decision B: UID is primary key internally)
  - find direct parents for a node
  - validate that the key column exists in the node before presence is checked

Traversal policy (clarified in step 2): the boundary for what can be checked
doesn't go by resource_type, but by whether the node has a queryable relation. Seeds,
sources and snapshots have relation_name and are checked like models. Ephemeral
models don't — they are skipped in checking, but traversed through.
"""

from __future__ import annotations

from pathlib import Path
import json
from dataclasses import dataclass, field
from typing import Any, Iterable

from .project import DbtProject, MetadataError

# --------------------------------------------------------------------------
# Policy
# --------------------------------------------------------------------------

TRAVERSABLE_TYPES = ("model", "seed", "source", "snapshot")

#: Nodes that never have parents. Traversal stops here regardless of policy.
TERMINAL_TYPES = ("seed", "source")


@dataclass(frozen=True)
class TraversalPolicy:
    """What the traversal is allowed to follow into.

    The defaults are 'on' because the failure mode of including a node is visible
    (one extra query, possibly a skipped_query_error in the report), while
    the failure mode of excluding it is a silent truncated answer.

    include_sources can be turned off against warehouses where an unfiltered count
    against a raw source table is expensive (BigQuery, Snowflake). Becomes
    --no-source-checks in step 8.
    """

    include_seeds: bool = True
    include_sources: bool = True
    include_snapshots: bool = True


# --------------------------------------------------------------------------
# Lasting
# --------------------------------------------------------------------------


def _load_json(path, what: str) -> dict[str, Any]:
    if not path.is_file():
        hint = (
            "Run 'dbt compile' or 'dbt build' in the project."
            if what == "manifest"
            else "Run 'dbt docs generate' in the project."
        )
        raise MetadataError(f"Did not find {what}: {path}. {hint}")
    try:
        with path.open(encoding="utf-8") as fh:
            return json.load(fh)
    except json.JSONDecodeError as exc:
        raise MetadataError(f"Could not parse {what} {path}: {exc}") from exc


def load_manifest(project: DbtProject) -> dict[str, Any]:
    return _load_json(project.manifest_file, "manifest")


def load_catalog(project: DbtProject, *, required: bool = True) -> dict[str, Any] | None:
    """Catalog is optional in the sense that presence still works without it.

    Without catalog we can't distinguish 'column doesn't exist' from 'we don't know'.
    Callers that can tolerate this send required=False and get None.
    """
    if not project.catalog_file.is_file():
        if required:
            raise MetadataError(
                f"Did not find catalog: {project.catalog_file}. "
                "Run 'dbt docs generate' in the project."
            )
        return None
    return _load_json(project.catalog_file, "catalog")


# --------------------------------------------------------------------------
# Node-oppslag
# --------------------------------------------------------------------------


def resource_type_of(node_uid: str) -> str:
    """Derive resource type from UID prefix. dbt guarantees the form
    '<type>.<package>.<name>' (sources have four parts).
    """
    return node_uid.split(".", 1)[0]


def lookup_node(manifest: dict[str, Any], node_uid: str) -> dict[str, Any]:
    """Sources are in manifest['sources'], not in manifest['nodes'].

    This is a simple but easily overlooked detail: seeds and snapshots are
    under 'nodes' together with models, sources are not.
    """
    bucket = "sources" if resource_type_of(node_uid) == "source" else "nodes"
    node = manifest.get(bucket, {}).get(node_uid)
    if node is None:
        raise MetadataError(f"Node '{node_uid}' does not exist in manifest ({bucket}).")
    return node


def find_node(
    manifest: dict[str, Any],
    name: str,
    resource_types: Iterable[str] = ("model",),
) -> str:
    """Decision B: only place short name -> UID happens.

    If 'name' is already a UID, it's just validated. Ambiguous short names
    (same model name in multiple packages) are rejected rather than guessed.
    """
    if resource_type_of(name) in TRAVERSABLE_TYPES:
        lookup_node(manifest, name)  # raises if unknown
        return name

    wanted = set(resource_types)
    matches: list[str] = []
    for bucket in ("nodes", "sources"):
        for uid, node in manifest.get(bucket, {}).items():
            if node.get("resource_type") in wanted and node.get("name") == name:
                matches.append(uid)

    if not matches:
        raise MetadataError(
            f"Found no {'/'.join(sorted(wanted))} with name or UID '{name}'."
        )
    if len(matches) > 1:
        raise MetadataError(
            f"Name '{name}' is ambiguous (multiple packages): "
            f"{', '.join(sorted(matches))}. Provide full node UID."
        )
    return matches[0]


def is_ephemeral(node: dict[str, Any]) -> bool:
    return node.get("config", {}).get("materialized") == "ephemeral"


def relation_for(manifest: dict[str, Any], node_uid: str) -> str:
    """Fully qualified and quoted relation name from dbt itself.

    We never build it from database/schema/alias — that would take on
    dialect and quoting responsibility dbt has already solved.
    """
    node = lookup_node(manifest, node_uid)
    relation = node.get("relation_name")
    if not relation:
        materialized = node.get("config", {}).get("materialized")
        raise MetadataError(
            f"Node '{node_uid}' lacks relation_name (materialization={materialized!r}). "
            "Ephemeral models cannot be presence-checked directly."
        )
    return relation


def display_name(manifest: dict[str, Any], node_uid: str) -> str:
    return lookup_node(manifest, node_uid).get("name", node_uid)


# --------------------------------------------------------------------------
# Catalog: kolonnevalidering (architectur.md §4)
# --------------------------------------------------------------------------

#: Outcome of catalog lookup for a node.
#:   ok            - node exists in catalog and has the key column
#:   column_missing- node exists in catalog, column doesn't -> skip
#:   node_absent   - node doesn't exist in catalog (stale/not built) -> check anyway
#:   no_catalog    - no catalog loaded at all -> check anyway
CATALOG_OK = "ok"
CATALOG_COLUMN_MISSING = "column_missing"
CATALOG_NODE_ABSENT = "node_absent"
CATALOG_NO_CATALOG = "no_catalog"


def catalog_entry(catalog: dict[str, Any] | None, node_uid: str) -> dict[str, Any] | None:
    if catalog is None:
        return None
    bucket = "sources" if resource_type_of(node_uid) == "source" else "nodes"
    return catalog.get(bucket, {}).get(node_uid)


def key_column_status(
    catalog: dict[str, Any] | None, node_uid: str, key_column: str
) -> tuple[str, str | None]:
    """Returns (status, resolved_type).

    The asymmetry is deliberate: 'node exists in catalog but column doesn't'
    is a certain negative and results in skipped_missing_key_column. 'Node doesn't
    exist in catalog' is lack of knowledge — typically because
    dbt docs generate wasn't run after the last build — and should not
    cut the node out of traversal. Then we check and add a warning.

    Column names are matched case-insensitively because catalog preserves warehouse
    casing (DuckDB lowercase, Snowflake uppercase).
    """
    if catalog is None:
        return CATALOG_NO_CATALOG, None

    entry = catalog_entry(catalog, node_uid)
    if entry is None:
        return CATALOG_NODE_ABSENT, None

    columns = entry.get("columns", {}) or {}
    for col_name, col in columns.items():
        if col_name.lower() == key_column.lower():
            return CATALOG_OK, (col.get("type") if isinstance(col, dict) else None)
    return CATALOG_COLUMN_MISSING, None


# --------------------------------------------------------------------------
# Foreldre-oppslag
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class NodeRef:
    """One node as the traversal layer needs to see it.

    skip_reason values are chosen to map directly to the presence enum
    in architectur.md §12.4 when the output contract is locked in step 7:
      missing_key_column -> skipped_missing_key_column
    """

    node_uid: str
    name: str
    resource_type: str
    relation: str | None
    checkable: bool
    traverse_through: bool
    terminal: bool
    skip_reason: str | None = None
    key_column_type: str | None = None
    catalog_status: str = CATALOG_NO_CATALOG
    warnings: tuple[str, ...] = field(default_factory=tuple)


def classify_node(
    manifest: dict[str, Any],
    catalog: dict[str, Any] | None,
    node_uid: str,
    key_column: str,
    policy: TraversalPolicy = TraversalPolicy(),
) -> NodeRef:
    """Determines if the node can be presence-checked, and if traversal should
    continue through it. Makes no queries.
    """
    rtype = resource_type_of(node_uid)
    node = lookup_node(manifest, node_uid)
    name = node.get("name", node_uid)
    terminal = rtype in TERMINAL_TYPES
    warnings: list[str] = []

    def _skip(reason: str, *, through: bool = False, rel: str | None = None) -> NodeRef:
        return NodeRef(
            node_uid=node_uid,
            name=name,
            resource_type=rtype,
            relation=rel,
            checkable=False,
            traverse_through=through,
            terminal=terminal,
            skip_reason=reason,
            catalog_status=CATALOG_NO_CATALOG if catalog is None else CATALOG_NODE_ABSENT,
            warnings=tuple(warnings),
        )

    if rtype not in TRAVERSABLE_TYPES:
        return _skip("unsupported_resource_type")
    if rtype == "seed" and not policy.include_seeds:
        return _skip("excluded_by_policy")
    if rtype == "source" and not policy.include_sources:
        return _skip("excluded_by_policy")
    if rtype == "snapshot" and not policy.include_snapshots:
        return _skip("excluded_by_policy", through=True)

    # Ephemeral: no relation to query against, but parents are still
    # relevant. Traverse through without checking.
    if rtype == "model" and is_ephemeral(node):
        return _skip("ephemeral", through=True)

    relation = node.get("relation_name")
    if not relation:
        return _skip("missing_relation", through=not terminal)

    status, coltype = key_column_status(catalog, node_uid, key_column)
    if status == CATALOG_COLUMN_MISSING:
        return NodeRef(
            node_uid=node_uid,
            name=name,
            resource_type=rtype,
            relation=relation,
            checkable=False,
            traverse_through=not terminal,
            terminal=terminal,
            skip_reason="missing_key_column",
            catalog_status=status,
            warnings=tuple(warnings),
        )
    if status == CATALOG_NODE_ABSENT:
        warnings.append(
            f"{name}: not in catalog.json — column validation skipped. "
            "Run 'dbt docs generate' if this is unexpected."
        )
    elif status == CATALOG_NO_CATALOG:
        warnings.append(f"{name}: no catalog loaded — column validation skipped.")

    return NodeRef(
        node_uid=node_uid,
        name=name,
        resource_type=rtype,
        relation=relation,
        checkable=True,
        traverse_through=not terminal,
        terminal=terminal,
        skip_reason=None,
        key_column_type=coltype,
        catalog_status=status,
        warnings=tuple(warnings),
    )


def parent_uids(manifest: dict[str, Any], node_uid: str) -> list[str]:
    """Raw depends_on.nodes, without filtering. Order from manifest is preserved."""
    node = lookup_node(manifest, node_uid)
    return list(node.get("depends_on", {}).get("nodes", []))


def parents_of(
    manifest: dict[str, Any],
    catalog: dict[str, Any] | None,
    node_uid: str,
    key_column: str,
    policy: TraversalPolicy = TraversalPolicy(),
) -> list[NodeRef]:
    """Direct parents, classified. Step 2 does only one level;
    step 3 calls this recursively.
    """
    if resource_type_of(node_uid) in TERMINAL_TYPES:
        return []
    return [
        classify_node(manifest, catalog, uid, key_column, policy)
        for uid in parent_uids(manifest, node_uid)
    ]

# --------------------------------------------------------------------------
# Compiled SQL (step 5+6: joins.py, filters.py)
# --------------------------------------------------------------------------

#: node['resource_type'] -> subdirectory in target/compiled/<package>/<here>/
_COMPILED_SUBDIR = {
    "model": "models",
    "snapshot": "snapshots",
}


def compiled_path_for_node(project: DbtProject, node: dict[str, Any]) -> "Path | None":
    subdir = _COMPILED_SUBDIR.get(node.get("resource_type", ""))
    rel_path = node.get("path")
    package_name = node.get("package_name")
    if subdir is None or not rel_path or not package_name:
        return None
    return project.project_dir / "target" / "compiled" / package_name / subdir / rel_path


def compiled_sql_for_node(
    project: DbtProject, node: dict[str, Any]
) -> tuple[str | None, str | None]:
    """Compiled SQL for one node.

    Reads from target/compiled/... on disk rather than manifest['compiled_code'].
    Reason (found during step 5): each 'dbt show --inline' (one per
    presence check) writes a fresh manifest.json as part of the command,
    but only compiles its own ad-hoc query — compiled_code for regular
    models disappears from manifest.json on disk again after a
    traversal. The files in target/compiled/ are unaffected by this.

    Returns (sql, source_note). source_note is None for normal file reading,
    set to an explanation on fallback to manifest or on error.
    """
    path = compiled_path_for_node(project, node)
    if path is not None and path.is_file():
        try:
            return path.read_text(encoding="utf-8"), None
        except OSError as exc:
            return None, f"Could not read {path}: {exc}"

    fallback = node.get("compiled_code")
    if fallback:
        note = (
            f"target/compiled file not found ({path}); used "
            "manifest['compiled_code'] instead. May be stale if "
            "traversal has run since last 'dbt compile'."
        )
        return fallback, note

    return None, None
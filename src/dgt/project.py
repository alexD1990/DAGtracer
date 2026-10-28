"""Load dbt artifacts and answer node and column metadata questions.

Models, seeds and snapshots live in `manifest['nodes']`; sources live in
`manifest['sources']`. The catalog mirrors that split with `catalog['nodes']`
and `catalog['sources']`. Relation names are always taken from the manifest
and never assembled from database/schema/identifier.
"""

from __future__ import annotations

import difflib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from dgt.errors import AmbiguousNodeError, ArtifactError, NodeNotFoundError

# Resource types a bare short name can match. Sources must be named as
# `source_name.table_name` or by unique_id.
SHORT_NAME_TYPES = frozenset({"model", "seed", "snapshot"})


@dataclass(frozen=True)
class Node:
    unique_id: str
    name: str
    resource_type: str
    package_name: str
    materialized: str | None
    relation_name: str | None

    @property
    def display_name(self) -> str:
        """Short name for output; sources render as `source_name.table_name`."""
        if self.resource_type == "source":
            return self.unique_id.split(".", 2)[2]
        return self.name

    @property
    def is_ephemeral(self) -> bool:
        return self.materialized == "ephemeral"


class DbtProject:
    """Parsed manifest and catalog for one dbt project."""

    def __init__(self, project_dir: Path, manifest: dict[str, Any], catalog: dict[str, Any]) -> None:
        self.project_dir = project_dir
        self.manifest = manifest
        self.catalog = catalog
        self._raw_nodes: dict[str, dict[str, Any]] = {
            **manifest.get("nodes", {}),
            **manifest.get("sources", {}),
        }

    @classmethod
    def load(cls, project_dir: Path) -> DbtProject:
        """Read `manifest.json` and `catalog.json` from the project's target path."""
        if not (project_dir / "dbt_project.yml").is_file():
            raise ArtifactError(f"no dbt_project.yml in {project_dir}")
        target_dir = project_dir / _target_path(project_dir)
        artifacts = []
        for filename in ("manifest.json", "catalog.json"):
            path = target_dir / filename
            if not path.is_file():
                raise ArtifactError(f"{path} not found; run `dbt docs generate` in {project_dir}")
            artifacts.append(_read_json(path))
        manifest, catalog = artifacts
        return cls(project_dir, manifest, catalog)

    def has_node(self, unique_id: str) -> bool:
        return unique_id in self._raw_nodes

    def node(self, unique_id: str) -> Node:
        try:
            raw = self._raw_nodes[unique_id]
        except KeyError:
            raise NodeNotFoundError(f"unknown node '{unique_id}'") from None
        return Node(
            unique_id=unique_id,
            name=raw["name"],
            resource_type=raw["resource_type"],
            package_name=raw.get("package_name", ""),
            materialized=raw.get("config", {}).get("materialized"),
            relation_name=raw.get("relation_name"),
        )

    def find_node(self, name: str) -> Node:
        """Resolve a unique_id, `source_name.table_name` or short name to a node.

        A short name matches models, seeds and snapshots only. A name that
        matches more than one node is rejected rather than guessed.
        """
        if name in self._raw_nodes:
            return self.node(name)

        matches = [uid for uid, raw in self._raw_nodes.items() if name in _names_for(uid, raw)]
        if len(matches) == 1:
            return self.node(matches[0])
        if matches:
            listed = "\n  ".join(sorted(matches))
            raise AmbiguousNodeError(
                f"'{name}' matches {len(matches)} nodes; use a unique_id:\n  {listed}"
            )

        candidates = sorted(
            {alias for uid, raw in self._raw_nodes.items() for alias in _names_for(uid, raw)}
        )
        hint = difflib.get_close_matches(name, candidates, n=3)
        suffix = f"; did you mean: {', '.join(hint)}?" if hint else ""
        raise NodeNotFoundError(f"no model, seed, snapshot or source named '{name}'{suffix}")

    def catalog_columns(self, unique_id: str) -> dict[str, str] | None:
        """Columns of a node per the catalog, keyed by lowercased name.

        Returns None when the catalog does not contain the node (ephemeral
        models, nodes never built). An empty dict means the node is in the
        catalog with no columns.
        """
        section = "sources" if unique_id.startswith("source.") else "nodes"
        entry = self.catalog.get(section, {}).get(unique_id)
        if entry is None:
            return None
        return {column.lower(): column for column in entry.get("columns", {})}

    @property
    def manifest_generated_at(self) -> datetime | None:
        return _generated_at(self.manifest)

    @property
    def catalog_generated_at(self) -> datetime | None:
        return _generated_at(self.catalog)

    def catalog_is_stale(self) -> bool:
        """True if the catalog was generated before the manifest."""
        manifest_ts, catalog_ts = self.manifest_generated_at, self.catalog_generated_at
        return manifest_ts is not None and catalog_ts is not None and catalog_ts < manifest_ts


def _names_for(unique_id: str, raw: dict[str, Any]) -> set[str]:
    """Names besides the unique_id a user may type for a node."""
    resource_type = raw.get("resource_type")
    if resource_type == "source":
        return {unique_id.split(".", 2)[2]}
    if resource_type in SHORT_NAME_TYPES:
        return {raw["name"]}
    return set()


def _target_path(project_dir: Path) -> str:
    try:
        project_yml = yaml.safe_load((project_dir / "dbt_project.yml").read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ArtifactError(f"{project_dir / 'dbt_project.yml'}: invalid YAML: {exc}") from exc
    return (project_yml or {}).get("target-path") or "target"


def _read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ArtifactError(f"cannot read {path}: {exc}") from exc


def _generated_at(artifact: dict[str, Any]) -> datetime | None:
    value = artifact.get("metadata", {}).get("generated_at")
    if not value:
        return None
    # fromisoformat accepts a trailing "Z" only from Python 3.11.
    return datetime.fromisoformat(value.replace("Z", "+00:00"))

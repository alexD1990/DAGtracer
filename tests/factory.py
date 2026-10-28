"""Build small synthetic manifests and catalogs for traversal tests."""

import json
import re
from pathlib import Path

from dgt.errors import DgtError

from dgt.project import DbtProject


def make_project(edges, columns, ephemeral=(), generated_at=("2024-01-01T00:00:00Z",) * 2):
    """`edges`: (parent, child) model names. `columns`: name -> column list, or
    None to leave the node out of the catalog. Names prefixed `seed:` or
    `source:` become seeds or sources (`source:raw.t`)."""
    names = {name for edge in edges for name in edge} | set(columns)
    uids = {name: _uid(name) for name in names}
    manifest = {
        "metadata": {"generated_at": generated_at[0]},
        "nodes": {},
        "sources": {},
        "parent_map": {uid: [] for uid in uids.values()},
        "child_map": {uid: [] for uid in uids.values()},
    }
    catalog = {"metadata": {"generated_at": generated_at[1]}, "nodes": {}, "sources": {}}
    for name, uid in uids.items():
        resource_type = uid.split(".", 1)[0]
        section = "sources" if resource_type == "source" else "nodes"
        short_name = uid.rsplit(".", 1)[1]
        manifest[section][uid] = {
            "name": short_name,
            "resource_type": resource_type,
            "config": {"materialized": "ephemeral" if name in ephemeral else "table"},
            "relation_name": None if name in ephemeral else f'"db"."main"."{short_name}"',
        }
        if columns.get(name) is not None:
            catalog[section][uid] = {"columns": {c: {"type": "TEXT"} for c in columns[name]}}
    for parent, child in edges:
        manifest["parent_map"][uids[child]].append(uids[parent])
        manifest["child_map"][uids[parent]].append(uids[child])
    return DbtProject(Path("."), manifest, catalog)


def _uid(name):
    if name.startswith("seed:"):
        return f"seed.p.{name[5:]}"
    if name.startswith("source:"):
        return f"source.p.{name[7:]}"
    return f"model.p.{name}"


class FakeShow:
    """Stands in for `dbt show`: answers value-count queries from a table.

    `counts`: node short name -> {value: count}; values not listed count 0. The SQL
    is parsed back, so query construction is exercised too. Queries against
    nodes in `fail` raise like a failing dbt show. `queries` records each
    relation queried.
    """

    _RELATION = re.compile(r' from "db"\."main"\."([^"]+)" where ')
    _COUNT = re.compile(r"= '([^']*)' then 1 end\) as (v\d+)")

    def __init__(self, counts, fail=()):
        self.counts = counts
        self.fail = set(fail)
        self.queries = []

    def __call__(self, sql):
        name = self._RELATION.search(sql).group(1)
        self.queries.append(name)
        if name in self.fail:
            raise DgtError(
                f"dbt show failed (exit 1)\nSQL: {sql}\n--- output ---\n"
                f"Runtime Error\n  Catalog Error: Table {name} does not exist"
            )
        counts = self.counts.get(name, {})
        row = {alias: counts.get(value, 0) for value, alias in self._COUNT.findall(sql)}
        return "noise before\n" + json.dumps({"show": [row]}, indent=2) + "\n"

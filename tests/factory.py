"""Build small synthetic manifests and catalogs for traversal tests."""

from pathlib import Path

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
        manifest[section][uid] = {
            "name": uid.rsplit(".", 1)[1],
            "resource_type": resource_type,
            "config": {"materialized": "ephemeral" if name in ephemeral else "table"},
            "relation_name": None if name in ephemeral else f'"db"."main"."{name}"',
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

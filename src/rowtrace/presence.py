"""Execution layer (architectur.md §3 layer 3, §6).

Metadata responsibility is moved to rowtrace/metadata.py in step 2. This file
now owns only the path from (relation, column, value) to a bool.

Decisions guiding the file (docs/byggeplan.md):
  A - check_presence(node_uid, key_column, value) -> bool is the interface.
      The internals (subprocess here) should be replaceable with dbtRunner without
      callers being touched. Reconsidered after step 3, not before.
  B - node UID is primary key internally.
  C - value is always str, and becomes SQL literal in exactly one function.
  E - project path and profile path are parameters, never constants.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import dataclass
from typing import Any

from .metadata import (
    TraversalPolicy,
    classify_node,
    load_catalog,
    load_manifest,
    relation_for,
)
from .project import DbtProject, MetadataError, PresenceError, RowtraceError

# Backward-compatible re-exports, so step1_test.py still works.
# New callers should import these from rowtrace.metadata / rowtrace.project.
from .metadata import find_node as find_model  # noqa: F401

__all__ = [
    "DbtProject",
    "PresenceError",
    "PresenceResult",
    "build_presence_sql",
    "check_presence",
    "check_presence_detailed",
    "check_presence_guarded",
    "parse_count",
    "quote_ident",
    "run_dbt_show",
    "sql_literal",
]


@dataclass(frozen=True)
class PresenceResult:
    """Detailed outcome. check_presence() boils this down to a bool."""

    node_uid: str
    relation: str
    present: bool
    count: int
    sql: str
    elapsed_s: float


@dataclass(frozen=True)
class GuardedResult:
    """Result where the node may have been skipped before querying.

    status follows presence-enumet i architectur.md §12.4 so langt det is
    definert i dag: present | absent | skipped_missing_key_column |
    skipped_query_error | unknown.
    """

    node_uid: str
    name: str
    status: str
    count: int | None = None
    relation: str | None = None
    sql: str | None = None
    elapsed_s: float | None = None
    key_column_type: str | None = None
    detail: str | None = None


# --------------------------------------------------------------------------
# Identifier and literal - the dialect-sensitive point
# --------------------------------------------------------------------------

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def quote_ident(name: str, style: str = "adapter") -> str:
    """Quote a column name.

    style="adapter" (default) generates Jinja that leans on dbt's egen
    adapter (architectur.md §6) - dbt show --inline compiles Jinja, so
    this already works from step 1. Don't hardcode quoting in Python.
    style="plain" gives raw double quotes, only for debugging by generated SQL.
    """
    if not _IDENT_RE.match(name):
        raise PresenceError(
            f"Invalid column name {name!r}. Allowed: letter/underscore followed by "
            "by letterer, digits or underscore."
        )
    if style == "adapter":
        return '{{ adapter.quote("' + name + '") }}'
    if style == "plain":
        return f'"{name}"'
    raise PresenceError(f"Unknown quote-style: {style!r}")


# Decision C: whitelist version. Deliberately restrictive - den is extended when a
# real project needs it, ikke in advance. Alt that ikke matches is rejected
# loudly rather than being escaped in guessed dialect-specific way.
_VALUE_RE = re.compile(r"^[A-Za-z0-9_\-.:+/ ]{1,256}$")


def sql_literal(value: str) -> str:
    """Only place en key value becomes to SQL. Don't duplicate this logic.

    Value is sent always as string literal. Warehouse casts itself against
    numeric columns; det is safer than that Python guesses the column type.
    resolved_type from catalog (step 2) exposed i output, but controls
    deliberately *ikke* literal form — se GuardedResult.key_column_type.
    """
    if not isinstance(value, str):
        raise PresenceError(
            f"Value must be str (decision C), got {type(value).__name__}."
        )
    if not _VALUE_RE.match(value):
        raise PresenceError(
            f"Value {value!r} contains characters outside whitelist "
            "(A-Z a-z 0-9 _ - . : + / space, max 256 characters)."
        )
    return f"'{value}'"


def build_presence_sql(
    relation: str, key_column: str, value: str, quote_style: str = "adapter"
) -> str:
    return (
        f"select count(*) as n from {relation} "
        f"where {quote_ident(key_column, quote_style)} = {sql_literal(value)}"
    )


# --------------------------------------------------------------------------
# Execution layer
# --------------------------------------------------------------------------


def _dbt_show_args(project: DbtProject, sql: str) -> list[str]:
    args = [
        "dbt",
        "show",
        "--inline",
        sql,
        "--output",
        "json",
        "--quiet",
        "--project-dir",
        str(project.project_dir),
    ]
    if project.profiles_dir is not None:
        args += ["--profiles-dir", str(project.profiles_dir)]
    if project.target is not None:
        args += ["--target", project.target]
    return args


def run_dbt_show(project: DbtProject, sql: str, timeout: int = 120) -> tuple[str, float]:
    """Run SQL via dbt. Returns (stdout, seconds used).

    cwd is set to project_dir because relative paths i profiles.yml (e.g.
    DuckDB sin 'path: testproject.duckdb') are resolved against working directory, ikke
    against --project-dir. Uten this points adapteren on wrong/empty database.
    """
    args = _dbt_show_args(project, sql)
    started = time.perf_counter()
    try:
        proc = subprocess.run(
            args,
            cwd=str(project.project_dir),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise PresenceError("'dbt' not found on PATH.") from exc
    except subprocess.TimeoutExpired as exc:
        raise PresenceError(f"dbt show timed out after {timeout}s.") from exc
    elapsed = time.perf_counter() - started

    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()[-1500:]
        raise PresenceError(
            f"dbt show failed (exit {proc.returncode}).\nSQL: {sql}\n--- output ---\n{tail}"
        )
    return proc.stdout, elapsed


def parse_count(stdout: str) -> int:
    """Extract the count value from dbt's JSON-output.

    dbt pretty-prints the object over multiple lines, so we can't parse
    line by line. Ikke re-introduce line by line parsing. I instead is scanned stdout after
    each '{' and is attempted decoded with raw_decode; first object with en
    row list wins.

    Value is fetched positionally (first column i first rad), because
    column nameets casing varies between warehouse.
    """
    decoder = json.JSONDecoder()
    idx = stdout.find("{")

    while idx != -1:
        try:
            payload, end = decoder.raw_decode(stdout, idx)
        except json.JSONDecodeError:
            idx = stdout.find("{", idx + 1)
            continue

        if isinstance(payload, dict):
            rows = payload.get("show")
            if rows is None:
                rows = payload.get("data")
            if isinstance(rows, list):
                if not rows:
                    return 0
                first = rows[0]
                raw = next(iter(first.values())) if isinstance(first, dict) else first
                try:
                    return int(raw)
                except (TypeError, ValueError) as exc:
                    raise PresenceError(f"Uexpected count-verdi from dbt: {raw!r}") from exc

        idx = stdout.find("{", end)

    raise PresenceError(
        "Fant no JSON-resultatobject i dbt's output.\n--- stdout ---\n"
        + stdout.strip()[-1500:]
    )


# --------------------------------------------------------------------------
# Decision A: det stabile grensesnittet
# --------------------------------------------------------------------------


def check_presence_detailed(
    node_uid: str,
    key_column: str,
    value: str,
    *,
    project: DbtProject,
    manifest: dict[str, Any] | None = None,
    quote_style: str = "adapter",
    timeout: int = 120,
) -> PresenceResult:
    manifest = manifest if manifest is not None else load_manifest(project)
    relation = relation_for(manifest, node_uid)
    sql = build_presence_sql(relation, key_column, value, quote_style)
    stdout, elapsed = run_dbt_show(project, sql, timeout=timeout)
    count = parse_count(stdout)
    return PresenceResult(
        node_uid=node_uid,
        relation=relation,
        present=count > 0,
        count=count,
        sql=sql,
        elapsed_s=elapsed,
    )


def check_presence(
    node_uid: str,
    key_column: str,
    value: str,
    *,
    project: DbtProject,
    manifest: dict[str, Any] | None = None,
    quote_style: str = "adapter",
    timeout: int = 120,
) -> bool:
    """Decision A. Signaturen holdes stabil; innmaten can byttes to
    dbtRunner or direct adapter-kall uten at kallere endres.
    """
    return check_presence_detailed(
        node_uid,
        key_column,
        value,
        project=project,
        manifest=manifest,
        quote_style=quote_style,
        timeout=timeout,
    ).present


def check_presence_guarded(
    node_uid: str,
    key_column: str,
    value: str,
    *,
    project: DbtProject,
    manifest: dict[str, Any],
    catalog: dict[str, Any] | None,
    policy: TraversalPolicy = TraversalPolicy(),
    quote_style: str = "adapter",
    timeout: int = 120,
) -> GuardedResult:
    """Presence-sjekk with catalog-validering foran (architectur.md §4).

    Dette is funksjonen the traversal i step 3 should kalle. Den kaster ikke
    on query error, but returns skipped_query_error, so that one unreadable
    node ikke tear down the entire the traversal. Tool error on metadata level
    kastes still.
    """
    ref = classify_node(manifest, catalog, node_uid, key_column, policy)

    if not ref.checkable:
        status = (
            "skipped_missing_key_column"
            if ref.skip_reason == "missing_key_column"
            else "unknown"
        )
        return GuardedResult(
            node_uid=node_uid,
            name=ref.name,
            status=status,
            relation=ref.relation,
            detail=ref.skip_reason,
        )

    sql = build_presence_sql(ref.relation, key_column, value, quote_style)
    try:
        stdout, elapsed = run_dbt_show(project, sql, timeout=timeout)
        count = parse_count(stdout)
    except RowtraceError as exc:
        return GuardedResult(
            node_uid=node_uid,
            name=ref.name,
            status="skipped_query_error",
            relation=ref.relation,
            sql=sql,
            key_column_type=ref.key_column_type,
            detail=str(exc)[:500],
        )

    return GuardedResult(
        node_uid=node_uid,
        name=ref.name,
        status="present" if count > 0 else "absent",
        count=count,
        relation=ref.relation,
        sql=sql,
        elapsed_s=elapsed,
        key_column_type=ref.key_column_type,
    )
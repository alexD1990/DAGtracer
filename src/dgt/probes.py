"""Probes that decide whether a column or values are present in a node.

ColumnProbe reads catalog.json only. ValueProbe counts values with one
`dbt show --inline` query per node and only runs where the column is present.
Prober combines both into a cached NodeState per node.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dgt.errors import DgtError
from dgt.findings import Status, UnknownReason
from dgt.project import DbtProject, Node

DBT_SHOW_TIMEOUT = 120


@dataclass(frozen=True)
class ProbeResult:
    status: Status
    unknown_reason: UnknownReason | None = None
    column_name: str | None = None
    column_type: str | None = None
    detail: str | None = None


@dataclass(frozen=True)
class NodeState:
    """Column status of a node plus, in value mode, the status of each value.

    Values are absent where the column is absent and unknown where the column
    is unknown; they are only queried where the column is present.
    """

    column: ProbeResult
    values: dict[str, Status] = field(default_factory=dict)

    @property
    def status(self) -> Status:
        return self.column.status


class ColumnProbe:
    """Column presence from catalog.json only; never queries the warehouse.

    A node in the catalog without the column is a certain `absent`. A node
    missing from the catalog is `unknown`, never `absent`. Results are cached
    per unique_id.
    """

    def __init__(self, project: DbtProject, column: str) -> None:
        self.project = project
        self.column = column
        self._cache: dict[str, ProbeResult] = {}

    def probe(self, node: Node) -> ProbeResult:
        cached = self._cache.get(node.unique_id)
        if cached is None:
            cached = self._cache[node.unique_id] = self._probe(node)
        return cached

    def _probe(self, node: Node) -> ProbeResult:
        if node.is_ephemeral:
            return ProbeResult(Status.UNKNOWN, UnknownReason.EPHEMERAL)
        columns = self.project.catalog_columns(node.unique_id)
        if columns is None:
            return ProbeResult(Status.UNKNOWN, UnknownReason.NOT_IN_CATALOG)
        name = columns.get(self.column.lower())
        if name is None:
            return ProbeResult(Status.ABSENT)
        return ProbeResult(
            Status.PRESENT,
            column_name=name,
            column_type=self.project.catalog_column_type(node.unique_id, name),
        )


class ValueProbe:
    """Count each value in a node's column with a single query.

    `run_sql` takes a SQL string and returns `dbt show --output json` stdout.
    """

    def __init__(self, values: Sequence[str], run_sql: Callable[[str], str]) -> None:
        self.values = list(values)
        self.run_sql = run_sql

    def probe(self, node: Node, column_name: str) -> dict[str, Status]:
        if node.relation_name is None:
            raise DgtError(f"{node.unique_id} has no relation_name to query")
        sql = build_value_sql(node.relation_name, column_name, self.values)
        counts = parse_counts(self.run_sql(sql), len(self.values))
        return {
            value: Status.PRESENT if count > 0 else Status.ABSENT
            for value, count in zip(self.values, counts)
        }


class Prober:
    """Cached NodeState per node; value mode when a ValueProbe is given.

    If the first value query of the run fails, the error is raised: it most
    likely affects every query (profile, connection, dbt itself). A later
    failure makes that node unknown with reason query_error, so it is
    traversed through like other unknown nodes.
    """

    def __init__(self, column_probe: ColumnProbe, value_probe: ValueProbe | None = None) -> None:
        self.column_probe = column_probe
        self.value_probe = value_probe
        self.values: list[str] = value_probe.values if value_probe else []
        self._cache: dict[str, NodeState] = {}
        self._query_succeeded = False

    def state(self, node: Node) -> NodeState:
        cached = self._cache.get(node.unique_id)
        if cached is None:
            cached = self._cache[node.unique_id] = self._state(node)
        return cached

    def probed(self) -> dict[str, NodeState]:
        """Every node probed so far, by unique_id."""
        return dict(self._cache)

    def _state(self, node: Node) -> NodeState:
        column = self.column_probe.probe(node)
        if column.status is not Status.PRESENT or self.value_probe is None:
            return NodeState(column, {value: column.status for value in self.values})
        try:
            values = self.value_probe.probe(node, column.column_name)
        except DgtError as exc:
            if not self._query_succeeded:
                raise DgtError(f"first value query failed ({node.unique_id}): {exc}") from exc
            failed = ProbeResult(
                Status.UNKNOWN,
                UnknownReason.QUERY_ERROR,
                column.column_name,
                column.column_type,
                detail=_summarize(str(exc)),
            )
            return NodeState(failed, {value: Status.UNKNOWN for value in self.values})
        self._query_succeeded = True
        return NodeState(column, values)


def _summarize(message: str, limit: int = 300) -> str:
    """First and last non-empty line of an error, truncated."""
    lines = [line.strip() for line in message.splitlines() if line.strip()]
    summary = lines[0] if len(lines) < 2 else f"{lines[0]} ... {lines[-1]}"
    return summary if len(summary) <= limit else summary[: limit - 3] + "..."


# Identifiers and literals: the dialect-sensitive part.

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# Deliberately restrictive; anything else is rejected rather than escaped in
# a guessed dialect-specific way.
_VALUE_RE = re.compile(r"^[A-Za-z0-9_\-.:+/ ]{1,256}$")


def quote_ident(name: str) -> str:
    """Quote a column name through the adapter, via Jinja compiled by `dbt show`."""
    if not _IDENT_RE.match(name):
        raise DgtError(
            f"invalid column name {name!r}: allowed is a letter or underscore followed by "
            "letters, digits or underscores"
        )
    return '{{ adapter.quote("' + name + '") }}'


def sql_literal(value: str) -> str:
    """The only place a user value becomes SQL.

    Values are always string literals; the warehouse casts them against
    numeric columns, which is safer than guessing the type in Python.
    """
    if not isinstance(value, str):
        raise DgtError(f"value must be a string, got {type(value).__name__}")
    if not _VALUE_RE.match(value):
        raise DgtError(
            f"value {value!r} contains characters outside the whitelist "
            "(A-Z a-z 0-9 _ - . : + / space, max 256 characters)"
        )
    return f"'{value}'"


def build_value_sql(relation: str, column: str, values: Sequence[str]) -> str:
    """One query counting every value; result columns are aliased v0..vN."""
    col = quote_ident(column)
    literals = [sql_literal(value) for value in values]
    counts = ", ".join(
        f"count(case when {col} = {literal} then 1 end) as v{index}"
        for index, literal in enumerate(literals)
    )
    return f"select {counts} from {relation} where {col} in ({', '.join(literals)})"


def parse_counts(stdout: str, expected: int) -> list[int]:
    """Read v0..v{expected-1} from `dbt show --output json` stdout.

    dbt pretty-prints the JSON object over several lines, so stdout is not
    parsed line by line: it is scanned from each '{' with raw_decode, and the
    first object holding a row list wins. Aliases are matched
    case-insensitively because some warehouses upper-case them.
    """
    rows = _show_rows(stdout)
    if not rows:
        return [0] * expected
    first = rows[0]
    if not isinstance(first, dict):
        raise DgtError(f"unexpected row from dbt show: {first!r}")
    by_alias = {str(key).lower(): value for key, value in first.items()}
    counts = []
    for index in range(expected):
        raw = by_alias.get(f"v{index}")
        try:
            counts.append(int(raw))
        except (TypeError, ValueError) as exc:
            raise DgtError(f"unexpected value for v{index} from dbt show: {raw!r}") from exc
    return counts


def _show_rows(stdout: str) -> list[Any]:
    decoder = json.JSONDecoder()
    index = stdout.find("{")
    while index != -1:
        try:
            payload, end = decoder.raw_decode(stdout, index)
        except json.JSONDecodeError:
            index = stdout.find("{", index + 1)
            continue
        if isinstance(payload, dict):
            rows = payload.get("show")
            if rows is None:
                rows = payload.get("data")
            if isinstance(rows, list):
                return rows
        index = stdout.find("{", end)
    raise DgtError(f"no JSON result in dbt show output:\n{stdout.strip()[-1500:]}")


# Running dbt.


def find_dbt_executable(override: str | None = None) -> str:
    """The dbt next to the running Python first (same virtualenv), then PATH."""
    if override:
        return override
    local = Path(sys.executable).parent / "dbt"
    if local.is_file():
        return str(local)
    found = shutil.which("dbt")
    if found is None:
        raise DgtError("dbt executable not found next to Python or on PATH")
    return found


@dataclass(frozen=True)
class DbtShow:
    """Runs `dbt --no-write-json show --inline` so target/ artifacts stay untouched.

    cwd is the project directory because relative paths in profiles.yml (such
    as DuckDB's `path:`) resolve against the working directory, not
    --project-dir.
    """

    executable: str
    project_dir: Path
    profiles_dir: Path | None = None
    target: str | None = None
    timeout: int = DBT_SHOW_TIMEOUT

    def args(self, sql: str) -> list[str]:
        args = [
            self.executable, "--no-write-json", "show",
            "--inline", sql,
            "--output", "json", "--quiet",
            "--project-dir", str(self.project_dir),
        ]
        if self.profiles_dir is not None:
            args += ["--profiles-dir", str(self.profiles_dir)]
        if self.target is not None:
            args += ["--target", self.target]
        return args

    def __call__(self, sql: str) -> str:
        try:
            proc = subprocess.run(
                self.args(sql),
                cwd=self.project_dir,
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
        except FileNotFoundError as exc:
            raise DgtError(f"cannot run dbt: {self.executable} not found") from exc
        except subprocess.TimeoutExpired as exc:
            raise DgtError(f"dbt show timed out after {self.timeout}s\nSQL: {sql}") from exc
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "").strip()[-1500:]
            raise DgtError(
                f"dbt show failed (exit {proc.returncode})\nSQL: {sql}\n--- output ---\n{tail}"
            )
        return proc.stdout

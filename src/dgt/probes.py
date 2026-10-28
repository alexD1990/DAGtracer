"""Probes that decide whether a column or values are present in a node."""

from __future__ import annotations

from dataclasses import dataclass

from dgt.findings import Status, UnknownReason
from dgt.project import DbtProject, Node


@dataclass(frozen=True)
class ProbeResult:
    status: Status
    unknown_reason: UnknownReason | None = None


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
        return ProbeResult(Status.PRESENT if self.column.lower() in columns else Status.ABSENT)

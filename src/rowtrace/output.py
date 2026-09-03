"""Steg 7 — JSON-output (architectur.md §12, revidert etter steg 1-6.5,
og rendyrket i steg 8.6 til ren presence-lineage).

Ren fold: TraceResult (steg 3) + BoundaryReport (steg 4) -> ett komplett
JSON-dokument. Ingen I/O, ingen klokke — started_at/finished_at gis av
kalleren.

SCHEMA_VERSION hevet til "2.0" i steg 8.6: diagnoselaget (join/filter,
steg 5/6/6.5) er koblet fra hovedstien (verktøyet gjør ingen vurdering av
funn lenger, kun presence). Dette fjerner 'conclusion', 'evidence',
'filters' og 'join' fra hvert boundary-objekt, og fjerner 'checked'-feltet
og include_checked-parameteren helt (default text-visning viser nå alt
sjekket per nivå uansett — se cli.py sin _render_text()). Diagnoselagets
kildekode (joins.py/filters.py/sqlparsing.py) er IKKE slettet — kun
koblet fra output.py og cli.py sin hovedsti. Se docs/byggeplan.md steg
8.6 for full begrunnelse.
SCHEMA_VERSION hevet til "2.1": nytt felt 'present_at_source', symmetrisk
med 'absent_at_source' — terminale noder (seed/source) hvor verdien faktisk
finnes. Additivt, ikke brytende: ingen eksisterende felt fjernet eller
endret betydning, kun nytt innhold lagt til toppnivådokumentet.
"""

from __future__ import annotations

from typing import Any

from .boundaries import (
    AbsentAtSourceFinding,
    BoundaryFinding,
    BoundaryReport,
    PresentAtSourceFinding,
    names_for_path,
)
from .traverse import (
    OUTCOME_COMPLETE,
    OUTCOME_MAX_DEPTH,
    OUTCOME_START_ERROR,
    OUTCOME_START_NOT_CHECKABLE,
    OUTCOME_START_PRESENT,
    TraceResult,
)

__all__ = ["SCHEMA_VERSION", "build_report"]

SCHEMA_VERSION = "2.1"

STATUS_COMPLETE = "complete"
STATUS_PARTIAL = "partial"
STATUS_FAILED = "failed"

_STATUS_FOR_OUTCOME = {
    OUTCOME_COMPLETE: STATUS_COMPLETE,
    OUTCOME_START_PRESENT: STATUS_COMPLETE,
    OUTCOME_MAX_DEPTH: STATUS_PARTIAL,
    OUTCOME_START_NOT_CHECKABLE: STATUS_FAILED,
    OUTCOME_START_ERROR: STATUS_FAILED,
}


def _status_for_outcome(outcome: str) -> str:
    return _STATUS_FOR_OUTCOME.get(outcome, STATUS_FAILED)


def _boundary_dict(finding: BoundaryFinding) -> dict[str, Any]:
    parents = [
        {
            "node": p.node_uid,
            "name": p.name,
            "resource_type": p.resource_type,
            "presence": p.status,
        }
        for p in finding.parents
    ]

    return {
        "node": finding.node_uid,
        "name": finding.name,
        "resource_type": finding.resource_type,
        "depth": finding.depth,
        "parents": parents,
    }


def _absent_at_source_dict(
    finding: AbsentAtSourceFinding, result: TraceResult
) -> dict[str, Any]:
    return {
        "node": finding.node_uid,
        "name": finding.name,
        "resource_type": finding.resource_type,
        "depth": finding.depth,
        "reached_via": list(finding.reached_via),
        "reached_via_names": list(names_for_path(result, finding.reached_via)),
    }

def _present_at_source_dict(
    finding: PresentAtSourceFinding, result: TraceResult
) -> dict[str, Any]:
    return {
        "node": finding.node_uid,
        "name": finding.name,
        "resource_type": finding.resource_type,
        "depth": finding.depth,
        "reached_via": list(finding.reached_via),
        "reached_via_names": list(names_for_path(result, finding.reached_via)),
    }


def build_report(
    result: TraceResult,
    boundary_report: BoundaryReport,
    *,
    tool_version: str,
    started_at: str,
    finished_at: str,
    target: str | None = None,
) -> dict[str, Any]:
    """Steg 7 sin kjernefunksjon. Ren fold, ingen I/O, ingen klokke."""
    start_node = result.checked.get(result.start_node)
    resolved_type = start_node.key_column_type if start_node else None

    return {
        "schema_version": SCHEMA_VERSION,
        "tool_version": tool_version,
        "status": _status_for_outcome(result.outcome),
        "outcome": result.outcome,
        "run": {
            "started_at": started_at,
            "finished_at": finished_at,
            "start_node": result.start_node,
            "keys": [
                {
                    "column": result.key_column,
                    "value": result.value,
                    "resolved_type": resolved_type,
                }
            ],
            "target": target,
            "stats": {
                "nodes_visited": result.stats.nodes_visited,
                "queries_executed": result.stats.queries_executed,
                "cache_hits": result.stats.cache_hits,
                "branches_pruned": result.stats.branches_pruned,
                "max_depth_reached": result.stats.max_depth_reached,
                "total_query_seconds": round(result.stats.total_query_seconds, 3),
            },
        },
        "boundaries": [_boundary_dict(f) for f in boundary_report.boundaries],
        "absent_at_source": [
            _absent_at_source_dict(f, result) for f in boundary_report.absent_at_source
        ],
        "present_at_source": [
            _present_at_source_dict(f, result) for f in boundary_report.present_at_source
        ],
        "warnings": list(result.warnings),
    }
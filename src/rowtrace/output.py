"""Step 7 — JSON output (architectur.md §12, revised after step 1-6.5,
and refined in step 8.6 to pure presence-lineage).

Pure fold: TraceResult (step 3) + BoundaryReport (step 4) -> one complete
JSON document. No I/O, no clock — started_at/finished_at given by
caller.

SCHEMA_VERSION raised to "2.0" in step 8.6: diagnostic layer (join/filter,
step 5/6/6.5) is disconnected from the main path (the tool makes no evaluation of
findings anymore, only presence). This removes 'conclusion', 'evidence',
'filters' and 'join' from each boundary object, and removes the 'checked' field
and include_checked parameter entirely (default text display now shows all
checked per level regardless — see cli.py's _render_text()). The diagnostic layer's
source code (joins.py/filters.py/sqlparsing.py) is NOT deleted — only
disconnected from output.py and cli.py's main path. See docs/byggeplan.md step
8.6 for full rationale.
SCHEMA_VERSION raised to "2.1": new field 'present_at_source', symmetric
with 'absent_at_source' — terminal nodes (seed/source) where the value actually
exists. Additive, not breaking: no existing field removed or
changed meaning, only new content added to the top-level document.
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
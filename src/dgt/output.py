"""Renderers for traversal reports: text, JSON and NDJSON."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import partial
from itertools import groupby
from typing import Any

from dgt.findings import Cause, Finding, ParentStatus, Report, Status, Trace, Visit
from dgt.graph import Direction
from dgt.project import Node

_DIRECTION_TITLES = {Direction.UP: "Upstream", Direction.DOWN: "Downstream"}
# Text-only grouping of downstream findings.
_DOWNSTREAM_GROUPS = (
    ("Reaches", {Cause.PRESENT_AT_LEAF, Cause.VALUE_PRESENT_AT_LEAF}),
    ("Stops at", {Cause.COLUMN_DROPPED, Cause.VALUE_DROPPED}),
    ("Appears at", {Cause.COLUMN_APPEARS, Cause.VALUE_APPEARS}),
    ("Absent at leaf", {Cause.COLUMN_ABSENT_AT_LEAF, Cause.VALUE_ABSENT_AT_LEAF}),
)
_NO_FINDINGS = (
    "no reached node changes status relative to the start node, and no branch ends "
    "at a seed, source or leaf with a known status."
)


def render_text(report: Report) -> str:
    start = report.start
    column_type = f" ({report.column_type})" if report.column_type else ""
    lines = [f"Column '{report.column}'{column_type} from {start.display_name} ({start.unique_id})"]
    if report.values:
        lines.append(f"Values: {', '.join(report.values)}")
    lines.append(
        "Start status: "
        + _status_with_values(report.start_status, report.start_values)
        + ("  [full trace]" if report.full_trace else "")
    )
    both = len(report.traces) > 1
    lines += _render_both(report) if both else _render_single(report.traces[0])

    findings = report.findings
    lines += ["", f"Findings ({len(findings)})"]
    if not findings:
        lines.append(f"  No findings: {_NO_FINDINGS}")
    for trace in report.traces:
        if not trace.findings:
            continue
        indent = ""
        if both:
            lines.append(f"  {_DIRECTION_TITLES[trace.direction]}")
            indent = "  "
        format_ = partial(_format_finding, tracked=report.values, with_direction=both)
        if trace.direction is Direction.UP:
            lines += [indent + line for f in trace.findings for line in format_(f)]
            continue
        for title, causes in _DOWNSTREAM_GROUPS:
            group = [f for f in trace.findings if f.cause in causes]
            if group:
                lines.append(f"{indent}  {title}")
                lines += [f"{indent}  {line}" for f in group for line in format_(f)]

    if report.warnings:
        lines += ["", "Warnings"]
        lines += [f"  {warning}" for warning in report.warnings]
    return "\n".join(lines) + "\n"


def _render_single(trace: Trace) -> list[str]:
    """One direction in data-flow order, levels -N..-1 or +1..+N."""
    visits = sorted(trace.visits, key=lambda visit: (visit.depth, visit.node.display_name))
    return ["", _DIRECTION_TITLES[trace.direction]] + (
        _render_levels(visits, visits) or ["  (no neighbors)"]
    )


def _render_both(report: Report) -> list[str]:
    """Both directions as one ladder of levels -N..0..+N, the start node at 0."""
    start = Visit(
        node=report.start,
        depth=0,
        status=report.start_status,
        via=None,
        column_type=report.column_type,
        values=report.start_values,
    )
    by_direction = {trace.direction: trace.visits for trace in report.traces}
    upstream = sorted(
        by_direction[Direction.UP], key=lambda v: (v.depth, v.node.display_name)
    )
    downstream = sorted(
        by_direction[Direction.DOWN], key=lambda v: (v.depth, v.node.display_name)
    )
    every = [*upstream, start, *downstream]
    return (
        ["", _DIRECTION_TITLES[Direction.UP]]
        + _render_levels(upstream, every)
        + _render_levels([start], every)
        + ["", _DIRECTION_TITLES[Direction.DOWN]]
        + (_render_levels(downstream, every) or ["  (no neighbors)"])
    )


def _render_levels(visits: list[Visit], aligned_with: list[Visit]) -> list[str]:
    """Visits grouped by level, in the given order; columns sized over `aligned_with`."""
    if not visits:
        return []
    name_width = max(len(visit.node.display_name) for visit in aligned_with)
    type_width = max(len(visit.column_type or "-") for visit in aligned_with)
    lines = []
    for depth, group in groupby(visits, key=lambda visit: visit.depth):
        label = _level(depth)
        for index, visit in enumerate(group):
            prefix = f"  {label:>3} " if index == 0 else "      "
            lines.append(prefix + _format_visit(visit, name_width, type_width))
    return lines


def _format_visit(visit: Visit, name_width: int, type_width: int) -> str:
    status = visit.status.value
    if visit.unknown_reason is not None:
        status += f" ({visit.unknown_reason.value})"
    line = (
        f"{visit.node.display_name:<{name_width}}  {visit.node.resource_type:<8}  "
        f"{visit.column_type or '-':<{type_width}}  {status:<9}"
    )
    values = _format_values(visit.status, visit.values)
    if values:
        line += f"  {values}"
    return (line + "  <- stop" if visit.stopped else line).rstrip()


def _level(depth: int) -> str:
    """Signed level label; the start node is level 0."""
    return f"{depth:+d}" if depth else "0"


def _format_finding(
    finding: Finding, tracked: tuple[str, ...], with_direction: bool = False
) -> list[str]:
    direction = f"{finding.direction.value:<4}  " if with_direction else ""
    head = (
        f"  {_level(finding.depth):>2}  {direction}{finding.cause.value:<23}  "
        f"{finding.node.display_name}"
    )
    if finding.values or (tracked and finding.cause is Cause.COLUMN_DROPPED):
        head += f" [{_format_finding_values(finding, tracked)}]"
    if finding.uncertain:
        head += f"  (uncertain: {', '.join(node.display_name for node in finding.candidates)})"
    lines = [head]
    if finding.parents:
        lines.append(f"        parents: {', '.join(map(_format_parent, finding.parents))}")
    return lines


# Findings whose values have mixed or noteworthy statuses, shown as value=status.
_WITH_STATUS = frozenset(
    {Cause.COLUMN_APPEARS, Cause.COLUMN_ABSENT_AT_SOURCE, Cause.COLUMN_ABSENT_AT_LEAF}
)


def _format_finding_values(finding: Finding, tracked: tuple[str, ...]) -> str:
    """Names of the values a finding is about, or value=status where status varies."""
    if finding.cause is Cause.COLUMN_DROPPED and not finding.values:
        return "no tracked values lost"
    if finding.cause not in _WITH_STATUS:
        return ", ".join(finding.values)
    statuses = set(finding.values.values())
    if statuses == {Status.ABSENT} and tuple(finding.values) == tracked:
        return "all values absent"
    return ", ".join(f"{value}={status.value}" for value, status in finding.values.items())


def _format_parent(parent: ParentStatus) -> str:
    details = _status_with_values(parent.status, parent.values)
    if parent.via:
        details += f", via {' <- '.join(node.display_name for node in parent.via)}"
    return f"{parent.node.display_name} ({details})"


def _status_with_values(status: Status, values: dict[str, Status]) -> str:
    formatted = _format_values(status, values)
    return f"{status.value}; {formatted}" if formatted else status.value


def _format_values(status: Status, values: dict[str, Status]) -> str:
    """Per-value statuses; only meaningful where the column is present."""
    if status is not Status.PRESENT or not values:
        return ""
    return ", ".join(f"{value}={value_status.value}" for value, value_status in values.items())


# JSON and NDJSON. The contract is described by schema/dgt-output-1.0.schema.json:
# bump the minor version for added fields, the major version for removed or
# changed ones.

SCHEMA_VERSION = "1.0"


@dataclass(frozen=True)
class RunMeta:
    """Run facts that are not part of the traversal result."""

    tool_version: str
    direction: str
    target: str | None
    started_at: str
    finished_at: str


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def run_status(report: Report) -> str:
    return "partial" if report.partial else "complete"


def build_document(report: Report, meta: RunMeta) -> dict[str, Any]:
    by_direction = {trace.direction: trace for trace in report.traces}
    return {
        "schema_version": SCHEMA_VERSION,
        "tool_version": meta.tool_version,
        "status": run_status(report),
        "run": {**_run_header(report, meta), "finished_at": meta.finished_at,
                "stats": _stats(report)},
        "traversals": {
            direction.value: (
                {"nodes": [_visit_json(v) for v in _ordered_visits(by_direction[direction])]}
                if direction in by_direction
                else None
            )
            for direction in (Direction.UP, Direction.DOWN)
        },
        "findings": [_finding_json(finding) for finding in report.findings],
        "warnings": list(report.warnings),
        "error": None,
    }


def error_document(message: str, meta: RunMeta) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "tool_version": meta.tool_version,
        "status": "error",
        "run": None,
        "traversals": {"up": None, "down": None},
        "findings": [],
        "warnings": [],
        "error": {"message": message},
    }


def render_json(document: dict[str, Any]) -> str:
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"


def render_ndjson(report: Report, meta: RunMeta) -> str:
    """Events in order: run_started, node_probed*, finding*, warning*, run_finished."""
    events: list[dict[str, Any]] = [
        {
            "event": "run_started",
            "schema_version": SCHEMA_VERSION,
            "tool_version": meta.tool_version,
            "run": _run_header(report, meta),
        }
    ]
    for trace in report.traces:
        events += [
            {"event": "node_probed", "direction": trace.direction.value, "node": _visit_json(v)}
            for v in _ordered_visits(trace)
        ]
    events += [{"event": "finding", "finding": _finding_json(f)} for f in report.findings]
    events += [{"event": "warning", "message": warning} for warning in report.warnings]
    events.append(
        {
            "event": "run_finished",
            "status": run_status(report),
            "finished_at": meta.finished_at,
            "stats": _stats(report),
            "error": None,
        }
    )
    return "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events)


def render_ndjson_error(message: str, meta: RunMeta) -> str:
    event = {
        "event": "run_finished",
        "status": "error",
        "finished_at": meta.finished_at,
        "stats": None,
        "error": {"message": message},
    }
    return json.dumps(event, ensure_ascii=False) + "\n"


def _run_header(report: Report, meta: RunMeta) -> dict[str, Any]:
    return {
        "start": {
            **_node_ref(report.start),
            "status": report.start_status.value,
            "values": _values_json(report.start_values),
        },
        "column": report.column,
        "column_type": report.column_type,
        "values": list(report.values),
        "direction": meta.direction,
        "full_trace": report.full_trace,
        "target": meta.target,
        "started_at": meta.started_at,
    }


def _stats(report: Report) -> dict[str, int]:
    findings = report.findings
    return {
        "nodes_visited": report.stats.nodes_visited,
        "nodes_probed": report.stats.nodes_probed,
        "value_queries": report.stats.value_queries,
        "query_errors": report.stats.query_errors,
        "findings": len(findings),
        "uncertain_findings": sum(finding.uncertain for finding in findings),
    }


def _ordered_visits(trace: Trace) -> list[Visit]:
    return sorted(trace.visits, key=lambda visit: (visit.depth, visit.node.unique_id))


def _node_ref(node: Node) -> dict[str, Any]:
    return {
        "unique_id": node.unique_id,
        "name": node.display_name,
        "resource_type": node.resource_type,
    }


def _values_json(values: dict[str, Status]) -> dict[str, str]:
    return {value: status.value for value, status in values.items()}


def _visit_json(visit: Visit) -> dict[str, Any]:
    return {
        **_node_ref(visit.node),
        "level": visit.depth,
        "status": visit.status.value,
        "values": _values_json(visit.values),
        "column_type": visit.column_type,
        "unknown_reason": visit.unknown_reason.value if visit.unknown_reason else None,
        "detail": visit.detail,
        "stopped": visit.stopped,
        "reached_from": visit.via,
    }


def _finding_json(finding: Finding) -> dict[str, Any]:
    return {
        "direction": finding.direction.value,
        "level": finding.depth,
        "cause": finding.cause.value,
        "node": _node_ref(finding.node),
        "values": _values_json(finding.values),
        "uncertain": finding.uncertain,
        "candidates": [node.unique_id for node in finding.candidates],
        "parents": [
            {
                **_node_ref(parent.node),
                "status": parent.status.value,
                "values": _values_json(parent.values),
                "via": [node.unique_id for node in parent.via],
            }
            for parent in finding.parents
        ],
    }

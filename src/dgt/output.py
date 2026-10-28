"""Renderers for traversal reports: text, JSON and NDJSON."""

from __future__ import annotations

from itertools import groupby

from dgt.findings import Cause, Finding, ParentStatus, Report, Status, Trace, Visit
from dgt.graph import Direction

_DIRECTION_TITLES = {Direction.UP: "Upstream", Direction.DOWN: "Downstream"}
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

    findings = [finding for trace in report.traces for finding in trace.findings]
    lines += ["", f"Findings ({len(findings)})"]
    lines += [
        line for finding in findings for line in _format_finding(finding, with_direction=both)
    ] or [f"  No findings: {_NO_FINDINGS}"]

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


def _format_finding(finding: Finding, with_direction: bool = False) -> list[str]:
    direction = f"{finding.direction.value:<4}  " if with_direction else ""
    head = (
        f"  {_level(finding.depth):>2}  {direction}{finding.cause.value:<23}  "
        f"{finding.node.display_name}"
    )
    if finding.values:
        if finding.cause is Cause.COLUMN_APPEARS:
            listed = ", ".join(f"{value}={status.value}" for value, status in finding.values.items())
        else:
            listed = ", ".join(finding.values)
        head += f" [{listed}]"
    if finding.uncertain:
        head += f"  (uncertain: {', '.join(node.display_name for node in finding.candidates)})"
    lines = [head]
    if finding.parents:
        lines.append(f"        parents: {', '.join(map(_format_parent, finding.parents))}")
    return lines


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

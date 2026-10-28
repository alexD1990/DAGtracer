"""Renderers for traversal reports: text, JSON and NDJSON."""

from __future__ import annotations

from itertools import groupby

from dgt.findings import Finding, Report, Trace, Visit
from dgt.graph import Direction

_DIRECTION_TITLES = {Direction.UP: "Upstream", Direction.DOWN: "Downstream"}


def render_text(report: Report) -> str:
    start = report.start
    lines = [
        f"Column '{report.column}' from {start.display_name} ({start.unique_id})",
        f"Start status: {report.start_status.value}"
        + ("  [full trace]" if report.full_trace else ""),
    ]
    for trace in report.traces:
        lines += ["", _DIRECTION_TITLES[trace.direction]]
        lines += _render_levels(trace) or ["  (no neighbors)"]

    findings = [finding for trace in report.traces for finding in trace.findings]
    lines += ["", f"Findings ({len(findings)})"]
    lines += [f"  {_format_finding(finding)}" for finding in findings] or ["  none"]

    if report.warnings:
        lines += ["", "Warnings"]
        lines += [f"  {warning}" for warning in report.warnings]
    return "\n".join(lines) + "\n"


def _render_levels(trace: Trace) -> list[str]:
    visits = sorted(trace.visits, key=lambda visit: (abs(visit.depth), visit.node.display_name))
    if not visits:
        return []
    name_width = max(len(visit.node.display_name) for visit in visits)
    lines = []
    for depth, group in groupby(visits, key=lambda visit: visit.depth):
        label = f"{depth:+d}"
        for index, visit in enumerate(group):
            prefix = f"  {label:>3} " if index == 0 else "      "
            lines.append(prefix + _format_visit(visit, name_width))
    return lines


def _format_visit(visit: Visit, name_width: int) -> str:
    status = visit.status.value
    if visit.unknown_reason is not None:
        status += f" ({visit.unknown_reason.value})"
    line = f"{visit.node.display_name:<{name_width}}  {visit.node.resource_type:<8}  {status:<9}"
    return (line + "  <- stop" if visit.stopped else line).rstrip()


def _format_finding(finding: Finding) -> str:
    edge = f"{finding.upstream.display_name} -> {finding.downstream.display_name}"
    value = f" [{finding.value}]" if finding.value is not None else ""
    return f"{finding.depth:+d}  {finding.cause.value:<15} {edge}{value}"

"""Breadth-first traversal of the DAG that records presence status changes.

From the start node, the traversal continues through nodes whose status
matches the status it arrived with and stops a branch at the first node where
the status changes; that edge is a finding. Unknown nodes (ephemeral, not in
catalog) inherit the arriving status and are traversed through. With
`full_trace`, branches never stop and every changing edge is reported.

Each node is probed and enqueued once, but every edge into it is checked, so
diamonds report one finding per changing edge.
"""

from __future__ import annotations

from collections import deque

from dgt.errors import DgtError
from dgt.findings import Cause, Finding, Report, Status, Trace, UnknownReason, Visit
from dgt.graph import DagGraph, Direction
from dgt.probes import ColumnProbe
from dgt.project import DbtProject, Node


def trace_column(
    project: DbtProject,
    graph: DagGraph,
    start: Node,
    column: str,
    directions: list[Direction],
    full_trace: bool = False,
) -> Report:
    probe = ColumnProbe(project, column)
    start_result = probe.probe(start)
    if start_result.status is Status.UNKNOWN:
        raise DgtError(
            f"status of column '{column}' in {start.unique_id} is unknown "
            f"({start_result.unknown_reason.value}); start from a node in catalog.json"
        )

    report = Report(
        start=start, column=column, start_status=start_result.status, full_trace=full_trace
    )
    if project.catalog_is_stale():
        report.warnings.append(
            f"catalog.json ({project.catalog_generated_at:%Y-%m-%d %H:%M:%S}) is older than "
            f"manifest.json ({project.manifest_generated_at:%Y-%m-%d %H:%M:%S}); "
            "run `dbt docs generate` to refresh column metadata"
        )
    for direction in directions:
        report.traces.append(
            _trace(project, graph, probe, start, start_result.status, direction, full_trace)
        )

    missing = sorted(
        {
            visit.node.unique_id
            for trace in report.traces
            for visit in trace.visits
            if visit.unknown_reason is UnknownReason.NOT_IN_CATALOG
        }
    )
    report.warnings.extend(
        f"{uid} is not in catalog.json; column status unknown, traversed through"
        for uid in missing
    )
    return report


def _trace(
    project: DbtProject,
    graph: DagGraph,
    probe: ColumnProbe,
    start: Node,
    start_status: Status,
    direction: Direction,
    full_trace: bool,
) -> Trace:
    step = -1 if direction is Direction.UP else 1
    trace = Trace(direction=direction)
    visits = {start.unique_id: Visit(start, 0, start_status, start_status, via=None)}
    queue = deque([start.unique_id])

    while queue:
        current = visits[queue.popleft()]
        if current.stopped:
            continue
        for uid in graph.neighbors(current.node.unique_id, direction):
            node = project.node(uid)
            result = probe.probe(node)
            changed = (
                result.status is not Status.UNKNOWN
                and result.status is not current.effective_status
            )
            if changed:
                trace.findings.append(
                    _finding(current, node, result.status, direction, current.depth + step)
                )
            if uid in visits:
                continue
            visits[uid] = Visit(
                node=node,
                depth=current.depth + step,
                status=result.status,
                effective_status=(
                    current.effective_status
                    if result.status is Status.UNKNOWN
                    else result.status
                ),
                via=current.node.unique_id,
                stopped=changed and not full_trace,
                unknown_reason=result.unknown_reason,
            )
            queue.append(uid)

    trace.visits = [visit for uid, visit in visits.items() if uid != start.unique_id]
    return trace


def _finding(
    current: Visit, node: Node, status: Status, direction: Direction, depth: int
) -> Finding:
    if direction is Direction.UP:
        upstream, downstream = node, current.node
        upstream_status = status
    else:
        upstream, downstream = current.node, node
        upstream_status = current.effective_status
    cause = Cause.COLUMN_DROPPED if upstream_status is Status.PRESENT else Cause.COLUMN_APPEARS
    return Finding(
        cause=cause, upstream=upstream, downstream=downstream, direction=direction, depth=depth
    )

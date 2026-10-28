"""Breadth-first traversal of the DAG that records presence status changes.

From the start node, the traversal continues through nodes whose status
matches the status it arrived with and stops a branch at the first node where
the status changes. Unknown nodes (ephemeral, not in catalog) inherit the
arriving status and are traversed through. With `full_trace`, branches never
stop. Each node is probed and enqueued once.

Where the traversal stops and what counts as a finding are separate: findings
are judged per node against the node's parents (see `Finding`), and only for
nodes the traversal continued through. A node where a branch stops is shown
but is not itself a finding.
"""

from __future__ import annotations

from collections import deque

from dgt.errors import DgtError
from dgt.findings import (
    Cause,
    Finding,
    ParentStatus,
    Report,
    Status,
    Trace,
    UnknownReason,
    Visit,
)
from dgt.graph import TERMINAL_UPSTREAM_TYPES, DagGraph, Direction
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

    for visit in visits.values():
        if visit.status is Status.UNKNOWN or (visit.stopped and not full_trace):
            continue
        finding = _judge(project, graph, probe, visit, direction)
        if finding is not None:
            trace.findings.append(finding)
    trace.findings.sort(key=lambda f: (abs(f.depth), f.node.display_name))
    trace.visits = [visit for uid, visit in visits.items() if uid != start.unique_id]
    return trace


def _judge(
    project: DbtProject, graph: DagGraph, probe: ColumnProbe, visit: Visit, direction: Direction
) -> Finding | None:
    """Classify a node with known status against its (resolved) parents' statuses."""
    node = visit.node

    def finding(
        cause: Cause, parents: tuple[ParentStatus, ...] = (), unknowns: list[Node] | None = None
    ) -> Finding:
        candidates = tuple(dict.fromkeys([node, *unknowns])) if unknowns else ()
        return Finding(
            cause, node, parents, direction, visit.depth,
            uncertain=bool(candidates), candidates=candidates,
        )

    if node.resource_type in TERMINAL_UPSTREAM_TYPES:
        return finding(Cause.PRESENT_AT_SOURCE) if visit.status is Status.PRESENT else None

    parents = _resolved_parents(project, graph, probe, node.unique_id)
    present = [parent for parent in parents if parent.status is Status.PRESENT]
    if visit.status is Status.ABSENT and present:
        if any(not parent.via for parent in present):
            return finding(Cause.COLUMN_DROPPED, parents)
        return finding(
            Cause.COLUMN_DROPPED, parents, [via for parent in present for via in parent.via]
        )
    if visit.status is Status.PRESENT and not present:
        unknowns = [via for parent in parents for via in parent.via]
        unknowns += [parent.node for parent in parents if parent.status is Status.UNKNOWN]
        return finding(Cause.COLUMN_APPEARS, parents, unknowns)
    return None


def _resolved_parents(
    project: DbtProject, graph: DagGraph, probe: ColumnProbe, unique_id: str
) -> tuple[ParentStatus, ...]:
    """Parents of a node, with unknown parents replaced by their own parents.

    Breadth-first, so a node reachable both directly and through unknown nodes
    is listed with its shortest `via`.
    """
    resolved: dict[str, ParentStatus] = {}
    expanded: set[str] = set()
    queue = deque((uid, ()) for uid in graph.neighbors(unique_id, Direction.UP))
    while queue:
        uid, via = queue.popleft()
        if uid in resolved or uid in expanded:
            continue
        node = project.node(uid)
        status = probe.probe(node).status
        grandparents = graph.neighbors(uid, Direction.UP)
        if status is Status.UNKNOWN and grandparents:
            expanded.add(uid)
            queue.extend((grandparent, (*via, node)) for grandparent in grandparents)
        else:
            resolved[uid] = ParentStatus(node, status, via)
    return tuple(resolved.values())

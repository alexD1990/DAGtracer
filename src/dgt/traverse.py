"""Breadth-first traversal of the DAG that records presence status changes.

The traversal tracks a set of keys: the column itself in column mode, or each
value in value mode. A key stays live on a branch while its status matches
the start node's; in value mode that requires the same column status as well
as the same value status, so column-level changes take precedence. A branch
continues while at least one key is live and stops at the first node where
none is. Unknown nodes (ephemeral, not in catalog, failed query) keep the
live keys they were reached with and are traversed through. With
`full_trace`, branches never stop. Each node is probed once and re-enqueued
only when reached again with keys it did not already carry live.

Where the traversal stops and what counts as a finding are separate: findings
are judged per node against all of the node's parents (see `Finding`).
Upstream, only nodes the traversal continued through are judged, for the keys
live there; a parent where a branch stops is shown but not judged. Downstream,
the child where a key changes is the finding, so each node is judged for the
keys that arrived at it, including where the branch stops. The start node is
judged against its parents only upstream, so `both` does not report it twice.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from functools import partial

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
from dgt.graph import DOWNSTREAM_TYPES, TERMINAL_UPSTREAM_TYPES, DagGraph, Direction
from dgt.probes import ColumnProbe, NodeState, Prober, ValueProbe
from dgt.project import DbtProject, Node

# Key that stands for the column itself in column mode.
COLUMN_KEY = None
Key = str | None


def trace_column(
    project: DbtProject,
    graph: DagGraph,
    start: Node,
    column: str,
    directions: list[Direction],
    full_trace: bool = False,
    value_probe: ValueProbe | None = None,
) -> Report:
    prober = Prober(ColumnProbe(project, column), value_probe)
    start_state = prober.state(start)
    if start_state.status is Status.UNKNOWN:
        raise DgtError(_unknown_start_message(project, graph, start, column))

    report = Report(
        start=start,
        column=column,
        start_status=start_state.status,
        full_trace=full_trace,
        values=tuple(prober.values),
        start_values=dict(start_state.values),
        column_type=start_state.column.column_type,
    )
    if project.catalog_is_stale():
        report.warnings.append(
            f"catalog.json ({project.catalog_generated_at:%Y-%m-%d %H:%M:%S}) is older than "
            f"manifest.json ({project.manifest_generated_at:%Y-%m-%d %H:%M:%S}); "
            "run `dbt docs generate` to refresh column metadata"
        )
    if Direction.DOWN in directions:
        report.warnings += _absent_start_warnings(start, column, start_state)
    tracer = _Tracer(project, graph, prober, start, full_trace)
    report.traces.extend(tracer.trace(direction) for direction in directions)

    for uid, state in sorted(prober.probed().items()):
        reason = state.column.unknown_reason
        if reason is UnknownReason.NOT_IN_CATALOG:
            report.warnings.append(
                f"{uid} is not in catalog.json; column status unknown, traversed through"
            )
        elif reason is UnknownReason.QUERY_ERROR:
            report.warnings.append(
                f"{uid}: value query failed; status unknown, traversed through: "
                f"{state.column.detail}"
            )
    return report


def _absent_start_warnings(start: Node, column: str, state: NodeState) -> list[str]:
    """Downstream from an absent column/value shows where it appears, not how far it spreads."""
    note = "downstream findings show where it appears or stays absent, not how far it spreads"
    if state.status is Status.ABSENT:
        return [f"column '{column}' is absent in {start.display_name}; {note}"]
    absent = [value for value, status in state.values.items() if status is Status.ABSENT]
    if absent:
        return [f"value(s) {', '.join(absent)} absent in {start.display_name}; {note}"]
    return []


def _unknown_start_message(project: DbtProject, graph: DagGraph, start: Node, column: str) -> str:
    message = f"status of column '{column}' in {start.unique_id} is unknown"
    if not start.is_ephemeral:
        return (
            f"{message}: the node is not in catalog.json; "
            f"run `dbt docs generate` in {project.project_dir}"
        )
    hints = []
    for label, direction in (("parent", Direction.UP), ("child", Direction.DOWN)):
        names = [
            node.display_name
            for node in map(project.node, graph.neighbors(start.unique_id, direction))
            if not node.is_ephemeral
        ]
        if names:
            hints.append(f"a {label} ({', '.join(names)})")
    suggestion = f"; start from {' or '.join(hints)} instead" if hints else ""
    return f"{message}: ephemeral models have no relation in catalog.json{suggestion}"


@dataclass
class _Entry:
    node: Node
    depth: int
    state: NodeState
    via: str | None
    arrived: set[Key]
    live: set[Key]


class _Tracer:
    def __init__(
        self, project: DbtProject, graph: DagGraph, prober: Prober, start: Node, full_trace: bool
    ) -> None:
        self.project = project
        self.graph = graph
        self.prober = prober
        self.start = start
        self.start_state = prober.state(start)
        self.full_trace = full_trace
        self.value_mode = bool(prober.values)
        self.keys: list[Key] = list(prober.values) if self.value_mode else [COLUMN_KEY]

    def trace(self, direction: Direction) -> Trace:
        step = -1 if direction is Direction.UP else 1
        start_uid = self.start.unique_id
        all_keys = set(self.keys)
        entries = {start_uid: _Entry(self.start, 0, self.start_state, None, all_keys, all_keys)}
        queue = deque([start_uid])

        while queue:
            current = entries[queue.popleft()]
            if not current.live and not self.full_trace:
                continue
            for uid in self.graph.neighbors(current.node.unique_id, direction):
                node = self.project.node(uid)
                state = self.prober.state(node)
                arrived = set(current.live)
                live = {key for key in arrived if self._unchanged(state, key)}
                entry = entries.get(uid)
                if entry is None:
                    entries[uid] = _Entry(
                        node, current.depth + step, state, current.node.unique_id, arrived, live
                    )
                    queue.append(uid)
                else:
                    entry.arrived |= arrived
                    if not live <= entry.live:
                        entry.live |= live
                        queue.append(uid)

        trace = Trace(direction=direction)
        for entry in entries.values():
            if entry.state.status is Status.UNKNOWN:
                continue
            if self.full_trace:
                judged = all_keys
            elif direction is Direction.UP:
                judged = entry.live
            else:
                judged = entry.arrived
            if judged:
                keys = [key for key in self.keys if key in judged]
                trace.findings.extend(self._judge(entry, keys, direction))
        # Data-flow order: most upstream level first.
        trace.findings.sort(
            key=lambda f: (f.depth, f.node.display_name, f.cause.value, f.uncertain)
        )
        trace.visits = [
            Visit(
                node=entry.node,
                depth=entry.depth,
                status=entry.state.status,
                via=entry.via,
                stopped=not entry.live and not self.full_trace,
                unknown_reason=entry.state.column.unknown_reason,
                column_type=entry.state.column.column_type,
                values=dict(entry.state.values),
                detail=entry.state.column.detail,
            )
            for uid, entry in entries.items()
            if uid != start_uid
        ]
        return trace

    def _unchanged(self, state: NodeState, key: Key) -> bool:
        """Whether a key keeps the start node's status in `state`; unknown always does."""
        if state.status is Status.UNKNOWN:
            return True
        if state.status is not self.start_state.status:
            return False
        if key is COLUMN_KEY or state.status is not Status.PRESENT:
            return True
        return state.values[key] is self.start_state.values[key]

    def _judge(self, entry: _Entry, keys: list[Key], direction: Direction) -> list[Finding]:
        """Classify a node with known status for the given keys."""
        if entry.node.resource_type in TERMINAL_UPSTREAM_TYPES:
            # Seeds and sources are only reached upstream (or are the start).
            return self._judge_source(entry, keys) if direction is Direction.UP else []
        findings = []
        if direction is Direction.UP or entry.node is not self.start:
            findings += self._judge_against_parents(entry, keys, direction)
        if direction is Direction.DOWN:
            findings += self._judge_leaf(entry)
        return findings

    def _finding(
        self,
        entry: _Entry,
        direction: Direction,
        cause: Cause,
        parents: tuple[ParentStatus, ...] = (),
        unknowns: list[Node] | None = None,
        affected: list[str] | None = None,
    ) -> Finding:
        node, state = entry.node, entry.state
        candidates = tuple(dict.fromkeys([node, *unknowns])) if unknowns else ()
        return Finding(
            cause, node, parents, direction, entry.depth,
            uncertain=bool(candidates), candidates=candidates,
            values={value: state.values[value] for value in affected or ()},
        )

    def _judge_source(self, entry: _Entry, keys: list[Key]) -> list[Finding]:
        state = entry.state
        finding = partial(self._finding, entry, Direction.UP)
        values = [key for key in keys if key is not COLUMN_KEY]
        present_here = [value for value in values if state.values[value] is Status.PRESENT]
        if state.status is Status.ABSENT:
            return [finding(Cause.COLUMN_ABSENT_AT_SOURCE)]
        if not self.value_mode:
            return [finding(Cause.PRESENT_AT_SOURCE)]
        absent_here = [value for value in values if value not in present_here]
        findings = []
        if present_here:
            findings.append(finding(Cause.VALUE_PRESENT_AT_SOURCE, affected=present_here))
        if absent_here:
            findings.append(finding(Cause.VALUE_ABSENT_AT_SOURCE, affected=absent_here))
        return findings

    def _judge_leaf(self, entry: _Entry) -> list[Finding]:
        """*_at_leaf findings for keys live at a model/snapshot with no such children."""
        node, state = entry.node, entry.state
        is_leaf = node.resource_type in DOWNSTREAM_TYPES and not self.graph.neighbors(
            node.unique_id, Direction.DOWN
        )
        if not is_leaf:
            return []
        live = set(self.keys) if self.full_trace else entry.live
        finding = partial(self._finding, entry, Direction.DOWN)
        if not self.value_mode:
            if COLUMN_KEY not in live:
                return []
            present = state.status is Status.PRESENT
            return [finding(Cause.PRESENT_AT_LEAF if present else Cause.COLUMN_ABSENT_AT_LEAF)]
        live_values = [value for value in self.keys if value in live]
        if not live_values:
            return []
        if state.status is Status.ABSENT:
            return [finding(Cause.COLUMN_ABSENT_AT_LEAF, affected=live_values)]
        present = [v for v in live_values if state.values[v] is Status.PRESENT]
        absent = [v for v in live_values if v not in present]
        findings = []
        if present:
            findings.append(finding(Cause.VALUE_PRESENT_AT_LEAF, affected=present))
        if absent:
            findings.append(finding(Cause.VALUE_ABSENT_AT_LEAF, affected=absent))
        return findings

    def _judge_against_parents(
        self, entry: _Entry, keys: list[Key], direction: Direction
    ) -> list[Finding]:
        """Dropped/appears findings for a node against all of its (resolved) parents."""
        node, state = entry.node, entry.state
        finding = partial(self._finding, entry, direction)
        values = [key for key in keys if key is not COLUMN_KEY]
        parents = self._resolved_parents(node.unique_id)
        with_column = [parent for parent in parents if parent.status is Status.PRESENT]

        if state.status is Status.ABSENT:
            if not with_column:
                return []
            unknowns = (
                []
                if any(not parent.via for parent in with_column)
                else [via for parent in with_column for via in parent.via]
            )
            affected = [
                value
                for value in values
                if any(parent.values[value] is Status.PRESENT for parent in with_column)
            ]
            return [finding(Cause.COLUMN_DROPPED, parents, unknowns, affected)]

        if not with_column:
            return [finding(Cause.COLUMN_APPEARS, parents, _unknowns_in(parents), values)]

        # Findings are grouped per (cause, uncertain): certain and uncertain
        # drops of different values at one node are reported separately.
        dropped_certain, dropped_uncertain, dropped_unknowns, appears = [], [], [], []
        for value in values:
            sources = [p for p in parents if p.values.get(value) is Status.PRESENT]
            if state.values[value] is Status.ABSENT and sources:
                if any(not parent.via for parent in sources):
                    dropped_certain.append(value)
                else:
                    dropped_uncertain.append(value)
                    dropped_unknowns += [via for parent in sources for via in parent.via]
            elif state.values[value] is Status.PRESENT and not sources:
                appears.append(value)

        findings = []
        if dropped_certain:
            findings.append(finding(Cause.VALUE_DROPPED, parents, None, dropped_certain))
        if dropped_uncertain:
            findings.append(
                finding(Cause.VALUE_DROPPED, parents, dropped_unknowns, dropped_uncertain)
            )
        if appears:
            findings.append(
                finding(Cause.VALUE_APPEARS, parents, _unknowns_in(parents), appears)
            )
        return findings

    def _resolved_parents(self, unique_id: str) -> tuple[ParentStatus, ...]:
        """Parents of a node, with unknown parents replaced by their own parents.

        Breadth-first, so a node reachable both directly and through unknown
        nodes is listed with its shortest `via`.
        """
        resolved: dict[str, ParentStatus] = {}
        expanded: set[str] = set()
        queue = deque((uid, ()) for uid in self.graph.neighbors(unique_id, Direction.UP))
        while queue:
            uid, via = queue.popleft()
            if uid in resolved or uid in expanded:
                continue
            node = self.project.node(uid)
            state = self.prober.state(node)
            grandparents = self.graph.neighbors(uid, Direction.UP)
            if state.status is Status.UNKNOWN and grandparents:
                expanded.add(uid)
                queue.extend((grandparent, (*via, node)) for grandparent in grandparents)
            else:
                resolved[uid] = ParentStatus(node, state.status, via, dict(state.values))
        return tuple(resolved.values())


def _unknowns_in(parents: tuple[ParentStatus, ...]) -> list[Node]:
    """Unknown nodes a judgment relied on: those passed through and unknown leaves."""
    unknowns = [via for parent in parents for via in parent.via]
    unknowns += [parent.node for parent in parents if parent.status is Status.UNKNOWN]
    return unknowns

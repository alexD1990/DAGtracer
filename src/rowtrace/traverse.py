"""Step 3 — traversal layer (architectur.md §3 layer 2, §5).

Algorithm (confirmed in step 3):
  0. Check start node. If the value is present there, the premise didn't hold —
     exit with dedicated outcome instead of an empty finding set.
  1. Find direct parents.
  2. Check which of them have the value.
  3. Parent with value -> stop the branch (pruned, but recorded).
  4. Parent without value -> repeat from 1 there, unless node
     already checked (cache).

BFS level by level, not DFS. --max-depth becomes a loop bound instead of a
recursion counter, progressive display gets complete levels to draw, and level
is the natural unit for parallelization in step 9.

Decision D: nothing here is an output contract. The on_event callback sends
loose dicts that *resemble* §12.5, but the schema is locked first in step 7.

Step 8.6: on_event events ('node_checked'/'branch_pruned') extended with
'name', 'reached_from' (node UID of the child that triggered the check) and
'terminal', so that cli.py's progressive text display can render correctly
as it goes, without having to wait for the entire TraceResult. Pure addition —
doesn't change traversal logic or TraceResult structure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from .metadata import TraversalPolicy, parents_of
from .presence import GuardedResult, check_presence_guarded
from .project import DbtProject

# --- Outcomes for the entire run ------------------------------------------
OUTCOME_COMPLETE = "complete"
OUTCOME_START_PRESENT = "start_present"
OUTCOME_START_NOT_CHECKABLE = "start_not_checkable"
OUTCOME_START_ERROR = "start_error"
OUTCOME_MAX_DEPTH = "partial_max_depth"

# --- Reasons why a branch stops --------------------------------------------
PRUNE_PRESENT = "present"
PRUNE_TERMINAL = "terminal"
PRUNE_MISSING_KEY_COLUMN = "missing_key_column"
PRUNE_QUERY_ERROR = "query_error"
PRUNE_EXCLUDED = "excluded_by_policy"
PRUNE_MAX_DEPTH = "max_depth"


@dataclass
class CheckedNode:
    """One visited node. Pruned nodes are also here — step 4 needs them
    to determine if the child is a boundary node (rule 2).
    """

    node_uid: str
    name: str
    resource_type: str
    status: str  # present|absent|unknown|skipped_missing_key_column|skipped_query_error
    depth: int
    from_cache: bool = False
    pruned: bool = False
    prune_reason: str | None = None
    terminal: bool = False
    expanded: bool = False
    count: int | None = None
    elapsed_s: float | None = None
    key_column_type: str | None = None
    detail: str | None = None
    queried: bool = False

    @property
    def absent_at_source(self) -> bool:
        """Terminal node (seed/source) without the value: row never entered."""
        return self.terminal and self.status == "absent"


@dataclass
class TraceStats:
    nodes_visited: int = 0
    queries_executed: int = 0
    cache_hits: int = 0
    branches_pruned: int = 0
    max_depth_reached: int = 0
    total_query_seconds: float = 0.0


@dataclass
class TraceResult:
    """Raw traversal result. Step 4 folds this to boundary nodes,
    step 7 to JSON. Deliberately no schema_version (decision D).
    """

    start_node: str
    key_column: str
    value: str
    outcome: str
    checked: dict[str, CheckedNode] = field(default_factory=dict)
    #: child -> parents, in manifest order. Foundation for
    #: diamond classification in step 4 and for paths in §12.3.
    edges: dict[str, list[str]] = field(default_factory=dict)
    stats: TraceStats = field(default_factory=TraceStats)
    warnings: list[str] = field(default_factory=list)

    def by_depth(self) -> list[list[CheckedNode]]:
        levels: dict[int, list[CheckedNode]] = {}
        for node in self.checked.values():
            levels.setdefault(node.depth, []).append(node)
        return [
            sorted(levels[d], key=lambda n: n.name) for d in sorted(levels)
        ]

    def absent_at_source(self) -> list[CheckedNode]:
        return [n for n in self.checked.values() if n.absent_at_source]


class Tracer:
    """Owns cache and stats per run. Nothing is module-global, so that
    two runs in the same process don't pollute each other.
    """

    def __init__(
        self,
        project: DbtProject,
        manifest: dict[str, Any],
        catalog: dict[str, Any] | None,
        *,
        policy: TraversalPolicy = TraversalPolicy(),
        use_cache: bool = True,
        max_depth: int | None = None,
        timeout: int = 120,
        full_trace: bool = False,
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.project = project
        self.manifest = manifest
        self.catalog = catalog
        self.policy = policy
        self.use_cache = use_cache
        self.max_depth = max_depth
        self.timeout = timeout
        self.full_trace = full_trace
        self.on_event = on_event
        self._cache: dict[str, GuardedResult] = {}

    # -- internals ---------------------------------------------------------

    def _emit(self, **event: Any) -> None:
        if self.on_event is not None:
            self.on_event(event)

    def _presence(self, node_uid: str, key: str, value: str) -> tuple[GuardedResult, bool]:
        """Returns (result, from_cache). Cache is per node UID, since
        key and value are fixed throughout the run (§5).
        """
        if self.use_cache and node_uid in self._cache:
            return self._cache[node_uid], True
        result = check_presence_guarded(
            node_uid,
            key,
            value,
            project=self.project,
            manifest=self.manifest,
            catalog=self.catalog,
            policy=self.policy,
            timeout=self.timeout,
        )
        if self.use_cache:
            self._cache[node_uid] = result
        return result, False

    def _prune_reason(self, res: GuardedResult, terminal: bool) -> str | None:
        if res.status == "present":
            return None if self.full_trace else PRUNE_PRESENT
        if res.status == "skipped_missing_key_column":
            return PRUNE_MISSING_KEY_COLUMN
        if res.status == "skipped_query_error":
            return PRUNE_QUERY_ERROR
        if terminal:
            return PRUNE_TERMINAL
        return None

    # -- public ------------------------------------------------------------

    def trace(self, start_uid: str, key_column: str, value: str) -> TraceResult:
        result = TraceResult(
            start_node=start_uid,
            key_column=key_column,
            value=value,
            outcome=OUTCOME_COMPLETE,
        )
        stats = result.stats

        self._emit(
            event="run_started",
            start_node=start_uid,
            key=key_column,
            value=value,
        )

        # --- Step 0: start node ------------------------------------------
        start_res, _ = self._presence(start_uid, key_column, value)
        stats.queries_executed += 1
        stats.nodes_visited += 1
        if start_res.elapsed_s:
            stats.total_query_seconds += start_res.elapsed_s

        start_node = CheckedNode(
            node_uid=start_uid,
            name=start_res.name,
            resource_type=start_uid.split(".", 1)[0],
            status=start_res.status,
            depth=0,
            count=start_res.count,
            elapsed_s=start_res.elapsed_s,
            key_column_type=start_res.key_column_type,
            detail=start_res.detail,
            queried=True,
        )
        result.checked[start_uid] = start_node
        self._emit(
            event="node_checked",
            node=start_uid,
            name=start_res.name,
            presence=start_res.status,
            depth=0,
            from_cache=False,
            terminal=False,
            elapsed_s=start_res.elapsed_s,
        )

        if start_res.status == "present":
            start_node.pruned = True
            start_node.prune_reason = PRUNE_PRESENT
            result.outcome = OUTCOME_START_PRESENT
            result.warnings.append(
                f"Value {value!r} exists in start node {start_res.name}. "
                "Traversal aborted, since the search premise doesn't hold. "
                "Check target/schema, that the project is rebuilt, and that "
                "the value is written as it appears in the table "
                f"(column type: {start_res.key_column_type or 'unknown'})."
            )
            self._emit(event="run_finished", status=result.outcome)
            return result

        if start_res.status in ("skipped_missing_key_column", "unknown"):
            result.outcome = OUTCOME_START_NOT_CHECKABLE
            result.warnings.append(
                f"Start node {start_res.name} could not be checked "
                f"({start_res.detail or start_res.status})."
            )
            self._emit(event="run_finished", status=result.outcome)
            return result

        if start_res.status == "skipped_query_error":
            result.outcome = OUTCOME_START_ERROR
            result.warnings.append(
                f"Query against start node {start_res.name} failed: {start_res.detail}"
            )
            self._emit(event="run_finished", status=result.outcome)
            return result

        # --- Step 1-4: BFS upward ----------------------------------------
        frontier = [start_uid]
        depth = 0
        expanded: set[str] = set()

        while frontier:
            depth += 1
            if self.max_depth is not None and depth > self.max_depth:
                result.outcome = OUTCOME_MAX_DEPTH
                for uid in frontier:
                    node = result.checked[uid]
                    node.pruned = True
                    node.prune_reason = PRUNE_MAX_DEPTH
                    stats.branches_pruned += 1
                result.warnings.append(
                    f"--max-depth {self.max_depth} reached. Traversal is incomplete."
                )
                break

            next_frontier: list[str] = []

            for child_uid in frontier:
                if child_uid in expanded:
                    continue
                expanded.add(child_uid)
                result.checked[child_uid].expanded = True

                parents = parents_of(
                    self.manifest, self.catalog, child_uid, key_column, self.policy
                )
                result.edges[child_uid] = [p.node_uid for p in parents]

                for ref in parents:
                    for warning in ref.warnings:
                        result.warnings.append(warning)

                    if ref.node_uid in result.checked:
                        existing = result.checked[ref.node_uid]

                        # Never queried (policy-excluded/missing column/
                        # ephemeral): nothing to cache or re-query, regardless
                        # of flag.
                        if not existing.queried:
                            stats.cache_hits += 1
                            self._emit(
                                event="node_checked", node=ref.node_uid,
                                name=existing.name,
                                presence=existing.status, depth=depth,
                                from_cache=True, terminal=existing.terminal,
                                reached_from=child_uid,
                                elapsed_s=None,
                            )
                            continue

                        if self.use_cache:
                            stats.cache_hits += 1
                            self._emit(
                                event="node_checked", node=ref.node_uid,
                                name=existing.name,
                                presence=existing.status, depth=depth,
                                from_cache=True, terminal=existing.terminal,
                                reached_from=child_uid,
                                elapsed_s=existing.elapsed_s,
                            )
                            continue

                        # --no-cache: revisit via another diamond branch
                        # should actually cost a new query. The node's
                        # recorded status and place in frontier were
                        # decided on first visit and don't change - this
                        # call exists only to make queries_executed
                        # honest about what an uncached run costs.
                        res, _ = self._presence(ref.node_uid, key_column, value)
                        stats.queries_executed += 1
                        if res.elapsed_s:
                            stats.total_query_seconds += res.elapsed_s
                        self._emit(
                            event="node_checked", node=ref.node_uid,
                            name=res.name,
                            presence=res.status, depth=depth, from_cache=False,
                            terminal=existing.terminal, reached_from=child_uid,
                            elapsed_s=res.elapsed_s,
                        )
                        continue

                    # Outside policy: record and stop branch (rule 5).
                    if not ref.checkable and not ref.traverse_through:
                        node = CheckedNode(
                            node_uid=ref.node_uid,
                            name=ref.name,
                            resource_type=ref.resource_type,
                            status="unknown"
                            if ref.skip_reason != "missing_key_column"
                            else "skipped_missing_key_column",
                            depth=depth,
                            pruned=True,
                            prune_reason=ref.skip_reason or PRUNE_EXCLUDED,
                            terminal=ref.terminal,
                            detail=ref.skip_reason,
                        )
                        result.checked[ref.node_uid] = node
                        stats.nodes_visited += 1
                        stats.branches_pruned += 1
                        self._emit(
                            event="branch_pruned",
                            node=ref.node_uid,
                            name=ref.name,
                            presence=node.status,
                            depth=depth,
                            terminal=ref.terminal,
                            reached_from=child_uid,
                            reason=node.prune_reason,
                        )
                        continue

                    # Ephemeral: no relation to query against, but parents
                    # are still relevant (rule 4).
                    if not ref.checkable and ref.traverse_through:
                        node = CheckedNode(
                            node_uid=ref.node_uid,
                            name=ref.name,
                            resource_type=ref.resource_type,
                            status="unknown",
                            depth=depth,
                            terminal=ref.terminal,
                            detail=ref.skip_reason,
                        )
                        result.checked[ref.node_uid] = node
                        stats.nodes_visited += 1
                        self._emit(
                            event="node_checked", node=ref.node_uid,
                            name=ref.name, presence=node.status, depth=depth,
                            from_cache=False, terminal=ref.terminal,
                            reached_from=child_uid,
                            elapsed_s=None,
                        )
                        next_frontier.append(ref.node_uid)
                        continue

                    res, from_cache = self._presence(ref.node_uid, key_column, value)
                    stats.nodes_visited += 1
                    if from_cache:
                        stats.cache_hits += 1
                    else:
                        stats.queries_executed += 1
                        if res.elapsed_s:
                            stats.total_query_seconds += res.elapsed_s

                    prune_reason = self._prune_reason(res, ref.terminal)
                    node = CheckedNode(
                        node_uid=ref.node_uid,
                        name=ref.name,
                        resource_type=ref.resource_type,
                        status=res.status,
                        depth=depth,
                        from_cache=from_cache,
                        pruned=prune_reason is not None,
                        prune_reason=prune_reason,
                        terminal=ref.terminal,
                        count=res.count,
                        elapsed_s=res.elapsed_s,
                        key_column_type=res.key_column_type,
                        detail=res.detail,
                        queried=True,
                    )
                    result.checked[ref.node_uid] = node

                    self._emit(
                        event="node_checked",
                        node=ref.node_uid,
                        name=ref.name,
                        presence=res.status,
                        depth=depth,
                        from_cache=from_cache,
                        terminal=ref.terminal,
                        reached_from=child_uid,
                        elapsed_s=res.elapsed_s,
                    )

                    if prune_reason is not None:
                        stats.branches_pruned += 1
                        self._emit(
                            event="branch_pruned",
                            node=ref.node_uid,
                            name=ref.name,
                            presence=res.status,
                            depth=depth,
                            terminal=ref.terminal,
                            reached_from=child_uid,
                            reason=prune_reason,
                            elapsed_s=res.elapsed_s,
                        )
                        continue

                    next_frontier.append(ref.node_uid)

            stats.max_depth_reached = max(stats.max_depth_reached, depth)
            frontier = next_frontier

        self._emit(
            event="run_finished",
            status=result.outcome,
            stats=vars(stats),
        )
        return result


def trace_upstream(
    start_uid: str,
    key_column: str,
    value: str,
    *,
    project: DbtProject,
    manifest: dict[str, Any],
    catalog: dict[str, Any] | None,
    policy: TraversalPolicy = TraversalPolicy(),
    use_cache: bool = True,
    max_depth: int | None = None,
    timeout: int = 120,
    full_trace: bool = False,
    on_event: Callable[[dict[str, Any]], None] | None = None,
) -> TraceResult:
    """Convenience function. Step 4 and CLI call this."""
    return Tracer(
        project,
        manifest,
        catalog,
        policy=policy,
        use_cache=use_cache,
        max_depth=max_depth,
        timeout=timeout,
        full_trace=full_trace,
        on_event=on_event,
    ).trace(start_uid, key_column, value)
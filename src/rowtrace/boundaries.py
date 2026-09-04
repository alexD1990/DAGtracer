"""Step 4 — boundary node identification (architectur.md §3 layer 4 preamble, §5, §7 opening, §15).

Pure fold over TraceResult from step 3 (checked + edges). No new dbt calls,
no new traversal — everything needed was already collected in step 3.

Two separate finding types, not one list with category tags (clarified in previous
conversation, cf. docs/byggeplan.md, step 4 section and "Open questions"):

  BoundaryFinding
      A node where the value is missing, but at least one direct parent has it
      (architectur.md §7, opening sentence). Has a list of parents with
      status — that's exactly the information step 5 (join analysis) and
      step 6 (filter check) need to classify the dropout further.
      The presence pattern alone does NOT determine join_confirmed/filter_suspected
      — that's step 5/6's job to parse compiled_code. Step 4 delivers only
      the structural candidates.

  AbsentAtSourceFinding
      The traversal followed the value all the way to a terminal node (seed/source)
      and it's still missing there. No parents to analyze — therefore no
      parents list, but instead reached_via: the path from start node down to
      source node, since that's the information that's useful when there's no
      join or filter to examine.

The diamond rule from architectur.md §5 follows directly from the definition above,
without separate code for each of the three cases:
  - All parents present, child absent  -> BoundaryFinding.
  - Some parents present, others absent, child absent -> BoundaryFinding
    (at least one present qualifies; step 5 decides later which of the
    absent parents structurally explains the dropout).
  - No parents present, child absent -> no finding here. Step 3 already continued
    traversal upward from each absent parent; either
    a BoundaryFinding shows up further up, or the chain ends in an
    AbsentAtSourceFinding.

A child can thus appear in one category while traversal simultaneously
continues past its absent parents and finds another finding further up
in the same run (confirmed manually: orgnr 222 gives prep_customer_orders as
BoundaryFinding AND raw_orders as AbsentAtSourceFinding, via stg_orders as
shared, absent parent).
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from .traverse import TraceResult

__all__ = [
    "ParentStatus",
    "BoundaryFinding",
    "AbsentAtSourceFinding",
    "PresentAtSourceFinding",
    "BoundaryReport",
    "fold_boundaries",
    "names_for_path",
]

@dataclass(frozen=True)
class ParentStatus:
    """One direct parent of a boundary node finding, as step 5/6 needs it."""

    node_uid: str
    name: str
    resource_type: str
    status: str  # same presence-enum that CheckedNode.status

    @property
    def present(self) -> bool:
        return self.status == "present"


@dataclass(frozen=True)
class BoundaryFinding:
    """A child where the value is missing, but at least one direct parent has it."""

    node_uid: str
    name: str
    resource_type: str
    depth: int
    parents: tuple[ParentStatus, ...]

    @property
    def present_parents(self) -> tuple[ParentStatus, ...]:
        return tuple(p for p in self.parents if p.present)

    @property
    def absent_parents(self) -> tuple[ParentStatus, ...]:
        """The parents step 5/6 should actually examine further (join/filter)."""
        return tuple(p for p in self.parents if not p.present)


@dataclass(frozen=True)
class AbsentAtSourceFinding:
    """Value reached a terminal node (seed/source) and still missing there."""

    node_uid: str
    name: str
    resource_type: str
    depth: int
    reached_via: tuple[str, ...]  # node-UID-is, startnode -> terminal node incl.

@dataclass(frozen=True)
class PresentAtSourceFinding:
    """Value reached a terminal node (seed/source) and exists there.

    Symmetric with AbsentAtSourceFinding. In normal (pruned) mode this
    only appears when a terminal node happens to be a direct parent
    of an absent child (it always gets checked there). With --full-trace
    (traverse.py) it becomes far more common, since pruning on present no
    longer stops the traversal before a terminal node is reached.
    """

    node_uid: str
    name: str
    resource_type: str
    depth: int
    reached_via: tuple[str, ...]  # node-UID-is, startnode -> terminal node incl.

@dataclass(frozen=True)
class BoundaryReport:
    """The two finding types from one TraceResult, kept separate (see module doc)."""

    boundaries: tuple[BoundaryFinding, ...]
    absent_at_source: tuple[AbsentAtSourceFinding, ...]
    present_at_source: tuple[PresentAtSourceFinding, ...]


def _shortest_path_uids(
    edges: dict[str, list[str]], start: str, target: str
) -> list[str] | None:
    """BFS from start along edges (child -> parents) to target.

    edges already has the direction step 3 followed (downstream node -> dens
    direct parents), so this is pure graph-BFS over data we already has
    — no new reading of manifest/catalog and no new queries.

    at multiple equally short paths (diamond) the one ... is chosen BFS finds first, that
    follows order in edges (dbt's depends_on order). Other valid
    paths with same length can exist — reached_via guarantees one valid path,
    ikke en kanonisk sti among several equivalent.
    """
    if start == target:
        return [start]

    visited = {start}
    previous: dict[str, str] = {}
    queue: deque[str] = deque([start])

    while queue:
        node = queue.popleft()
        for parent in edges.get(node, []):
            if parent in visited:
                continue
            visited.add(parent)
            previous[parent] = node
            if parent == target:
                path = [target]
                cur = target
                while cur != start:
                    cur = previous[cur]
                    path.append(cur)
                path.reverse()
                return path
            queue.append(parent)

    return None  # should not occur: target is fetched from result.checked selv


def fold_boundaries(result: TraceResult) -> BoundaryReport:
    """step 4 sin core function. O(noder + edges), no I/O.

    Prerequisite: result comes from Tracer.trace()/trace_upstream() i step 3
    (traverse.py) and is not mutated in the meantime.
    """
    boundaries: list[BoundaryFinding] = []

    for child_uid, parent_uids in result.edges.items():
        child = result.checked.get(child_uid)
        if child is None or child.status != "absent":
            continue

        parents = tuple(
            ParentStatus(
                node_uid=p_uid,
                name=result.checked[p_uid].name,
                resource_type=result.checked[p_uid].resource_type,
                status=result.checked[p_uid].status,
            )
            for p_uid in parent_uids
            if p_uid in result.checked
        )
        if not any(p.present for p in parents):
            continue  # §5: no parents present -> not a finding her

        boundaries.append(
            BoundaryFinding(
                node_uid=child_uid,
                name=child.name,
                resource_type=child.resource_type,
                depth=child.depth,
                parents=parents,
            )
        )

    absent_at_source: list[AbsentAtSourceFinding] = []
    for node in result.absent_at_source():
        path = _shortest_path_uids(result.edges, result.start_node, node.node_uid)
        reached_via = tuple(path) if path is not None else (result.start_node, node.node_uid)
        absent_at_source.append(
            AbsentAtSourceFinding(
                node_uid=node.node_uid,
                name=node.name,
                resource_type=node.resource_type,
                depth=node.depth,
                reached_via=reached_via,
            )
        )

    present_at_source: list[PresentAtSourceFinding] = []
    for node in result.checked.values():
        if not (node.terminal and node.status == "present"):
            continue
        path = _shortest_path_uids(result.edges, result.start_node, node.node_uid)
        reached_via = tuple(path) if path is not None else (result.start_node, node.node_uid)
        present_at_source.append(
            PresentAtSourceFinding(
                node_uid=node.node_uid,
                name=node.name,
                resource_type=node.resource_type,
                depth=node.depth,
                reached_via=reached_via,
            )
        )

    boundaries.sort(key=lambda f: (f.depth, f.name))
    absent_at_source.sort(key=lambda f: (f.depth, f.name))
    present_at_source.sort(key=lambda f: (f.depth, f.name))

    return BoundaryReport(
        boundaries=tuple(boundaries),
        absent_at_source=tuple(absent_at_source),
        present_at_source=tuple(present_at_source),
    )


def names_for_path(result: TraceResult, path: tuple[str, ...]) -> tuple[str, ...]:
    """Visningshjelp: oversett en reached_via-sti (UID-is) to kortnavn."""
    return tuple(
        result.checked[uid].name if uid in result.checked else uid.split(".")[-1]
        for uid in path
    )
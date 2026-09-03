"""Steg 3 — traverseringslag (architectur.md §3 lag 2, §5).

Algoritmen (bekreftet i steg 3):
  0. Sjekk startnoden. Er verdien til stede der, holdt ikke premisset —
     avslutt med eget utfall framfor et tomt funn-sett.
  1. Finn direkte foreldre.
  2. Sjekk hvilke av dem som har verdien.
  3. Forelder med verdien -> stopp grenen (prunes, men registreres).
  4. Forelder uten verdien -> gjenta fra 1 derfra, med mindre noden
     allerede er sjekket (cache).

BFS nivå for nivå, ikke DFS. --max-depth blir en løkkegrense framfor en
rekursjonsteller, progressiv visning får ferdige nivåer å tegne, og nivået
er den naturlige enheten for parallellisering i steg 9.

Beslutning D: ingenting her er en outputkontrakt. on_event-callbacken sender
løse dicts som *ligner* §12.5, men skjemaet låses først i steg 7.

Steg 8.6: on_event-hendelsene ('node_checked'/'branch_pruned') utvidet med
'name', 'reached_from' (node-UID til barnet som utløste sjekken) og
'terminal', slik at cli.py sin progressive tekstvisning kan rendre riktig
underveis, uten å måtte vente på hele TraceResult. Rent tillegg — endrer
ikke traverseringslogikken eller TraceResult sin struktur.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from .metadata import TraversalPolicy, parents_of
from .presence import GuardedResult, check_presence_guarded
from .project import DbtProject

# --- Utfall for hele kjøringen --------------------------------------------
OUTCOME_COMPLETE = "complete"
OUTCOME_START_PRESENT = "start_present"
OUTCOME_START_NOT_CHECKABLE = "start_not_checkable"
OUTCOME_START_ERROR = "start_error"
OUTCOME_MAX_DEPTH = "partial_max_depth"

# --- Grunner til at en gren stopper ---------------------------------------
PRUNE_PRESENT = "present"
PRUNE_TERMINAL = "terminal"
PRUNE_MISSING_KEY_COLUMN = "missing_key_column"
PRUNE_QUERY_ERROR = "query_error"
PRUNE_EXCLUDED = "excluded_by_policy"
PRUNE_MAX_DEPTH = "max_depth"


@dataclass
class CheckedNode:
    """Én besøkt node. Prunede noder ligger her også — steg 4 trenger dem
    for å avgjøre om barnet er en grensenode (regel 2).
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
        """Terminal node (seed/source) uten verdien: raden kom aldri inn."""
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
    """Rått traverseringsresultat. Steg 4 folder dette til grensenoder,
    steg 7 til JSON. Bevisst ingen schema_version (beslutning D).
    """

    start_node: str
    key_column: str
    value: str
    outcome: str
    checked: dict[str, CheckedNode] = field(default_factory=dict)
    #: barn -> foreldre, i manifestets rekkefølge. Grunnlaget for
    #: diamantklassifiseringen i steg 4 og for paths i §12.3.
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
    """Eier cache og stats per kjøring. Ingenting er modulglobalt, slik at
    to kjøringer i samme prosess ikke forurenser hverandre.
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
        """Returnerer (resultat, from_cache). Cache er per node-UID, siden
        nøkkel og verdi er faste gjennom kjøringen (§5).
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

        # --- Steg 0: startnoden ------------------------------------------
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
                f"Verdien {value!r} finnes i startnoden {start_res.name}. "
                "Traverseringen er avbrutt, siden premisset for søket ikke holder. "
                "Sjekk target/schema, at prosjektet er bygget på nytt, og at "
                "verdien er skrevet slik den står i tabellen "
                f"(kolonnetype: {start_res.key_column_type or 'ukjent'})."
            )
            self._emit(event="run_finished", status=result.outcome)
            return result

        if start_res.status in ("skipped_missing_key_column", "unknown"):
            result.outcome = OUTCOME_START_NOT_CHECKABLE
            result.warnings.append(
                f"Startnoden {start_res.name} kunne ikke sjekkes "
                f"({start_res.detail or start_res.status})."
            )
            self._emit(event="run_finished", status=result.outcome)
            return result

        if start_res.status == "skipped_query_error":
            result.outcome = OUTCOME_START_ERROR
            result.warnings.append(
                f"Spørringen mot startnoden {start_res.name} feilet: {start_res.detail}"
            )
            self._emit(event="run_finished", status=result.outcome)
            return result

        # --- Steg 1-4: BFS oppover ---------------------------------------
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
                    f"--max-depth {self.max_depth} nådd. Traverseringen er ufullstendig."
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

                        # Aldri spurt (policy-ekskludert/manglende kolonne/
                        # efemer): ingenting å cache eller re-spørre, uansett
                        # flagg.
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

                        # --no-cache: gjenbesøk via en annen diamantgren
                        # skal faktisk koste en ny spørring. Nodens
                        # registrerte status og plass i frontier ble
                        # avgjort ved første besøk og endres ikke - dette
                        # kallet finnes kun for å gjøre queries_executed
                        # ærlig om hva en ucachet kjøring koster.
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

                    # Utenfor policy: registrer og stopp grenen (regel 5).
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

                    # Efemer: ingen relasjon å spørre mot, men foreldrene
                    # er fortsatt relevante (regel 4).
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
    """Bekvemmelighetsfunksjon. Steg 4 og CLI kaller denne."""
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
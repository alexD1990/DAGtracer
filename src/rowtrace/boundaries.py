"""Steg 4 — grensenode-identifisering (architectur.md §3 lag 4 forspill, §5, §7 åpning, §15).

Ren fold over TraceResult fra steg 3 (checked + edges). Ingen nye dbt-kall,
ingen ny traversering — alt som trengs ble allerede samlet inn i steg 3.

To separate funn-typer, ikke én liste med kategori-tag (avklart i forrige
samtale, jf. docs/byggeplan.md, steg 4-seksjonen og "Åpne spørsmål"):

  BoundaryFinding
      En node der verdien mangler, men minst én direkte forelder har den
      (architectur.md §7, åpningssetningen). Har en liste av foreldre med
      status — det er nøyaktig informasjonen steg 5 (join-analyse) og
      steg 6 (filtersjekk) trenger for å klassifisere bortfallet videre.
      Presence-mønsteret alene avgjør IKKE join_confirmed/filter_suspected
      — det er steg 5/6 sin jobb å parse compiled_code. Steg 4 leverer kun
      de strukturelle kandidatene.

  AbsentAtSourceFinding
      Traverseringen fulgte verdien helt til en terminal node (seed/source)
      og den mangler fortsatt der. Ingen foreldre å analysere — derfor ingen
      parents-liste, men i stedet reached_via: stien fra startnoden ned til
      kildenoden, siden det er den informasjonen som er nyttig når det ikke
      finnes noe join eller filter å undersøke.

Diamantregelen fra architectur.md §5 følger direkte av definisjonen over,
uten egen kode for hvert av de tre tilfellene:
  - Alle foreldre present, barn absent  -> BoundaryFinding.
  - Noen foreldre present, andre absent, barn absent -> BoundaryFinding
    (minst én present kvalifiserer; steg 5 avgjør senere hvilken av de
    fraværende foreldrene som strukturelt forklarer bortfallet).
  - Ingen foreldre present, barn absent -> intet funn her. Steg 3 fortsatte
    allerede traverseringen oppover fra hver fraværende forelder; enten
    dukker et BoundaryFinding opp lenger oppe, eller kjeden ender i et
    AbsentAtSourceFinding.

Et barn kan altså dukke opp i én kategori mens traverseringen samtidig
fortsetter forbi dets fraværende foreldre og finner et funn til lenger oppe
i samme kjøring (bekreftet manuelt: orgnr 222 gir prep_customer_orders som
BoundaryFinding OG raw_orders som AbsentAtSourceFinding, via stg_orders som
delt, fraværende forelder).
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
    """Én direkte forelder til et grensenode-funn, slik steg 5/6 trenger den."""

    node_uid: str
    name: str
    resource_type: str
    status: str  # samme presence-enum som CheckedNode.status

    @property
    def present(self) -> bool:
        return self.status == "present"


@dataclass(frozen=True)
class BoundaryFinding:
    """Et barn der verdien mangler, men minst én direkte forelder har den."""

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
        """Foreldrene steg 5/6 faktisk skal undersøke videre (join/filter)."""
        return tuple(p for p in self.parents if not p.present)


@dataclass(frozen=True)
class AbsentAtSourceFinding:
    """Verdien nådde en terminal node (seed/source) og mangler fortsatt der."""

    node_uid: str
    name: str
    resource_type: str
    depth: int
    reached_via: tuple[str, ...]  # node-UID-er, startnode -> terminal node inkl.

@dataclass(frozen=True)
class PresentAtSourceFinding:
    """Verdien nådde en terminal node (seed/source) og finnes der.

    Symmetrisk med AbsentAtSourceFinding. I normal (prunet) modus dukker
    denne kun opp når en terminal node tilfeldigvis er en direkte forelder
    til et absent barn (den blir jo alltid sjekket der). Med --full-trace
    (traverse.py) blir den langt vanligere, siden pruning på present ikke
    lenger stopper traverseringen før en terminal node nås.
    """

    node_uid: str
    name: str
    resource_type: str
    depth: int
    reached_via: tuple[str, ...]  # node-UID-er, startnode -> terminal node inkl.

@dataclass(frozen=True)
class BoundaryReport:
    """De to funn-typene fra én TraceResult, holdt atskilt (se moduldoc)."""

    boundaries: tuple[BoundaryFinding, ...]
    absent_at_source: tuple[AbsentAtSourceFinding, ...]
    present_at_source: tuple[PresentAtSourceFinding, ...]


def _shortest_path_uids(
    edges: dict[str, list[str]], start: str, target: str
) -> list[str] | None:
    """BFS fra start langs edges (barn -> foreldre) fram til target.

    edges har allerede retningen steg 3 fulgte (nedstrøms node -> dens
    direkte foreldre), så dette er ren graf-BFS over data vi allerede har
    — ingen ny lesning av manifest/catalog og ingen nye spørringer.

    Ved flere like korte stier (diamant) velges den BFS finner først, som
    følger rekkefølgen i edges (dbt sin depends_on-rekkefølge). Andre gyldige
    stier med samme lengde kan finnes — reached_via garanterer én gyldig sti,
    ikke en kanonisk sti blant flere likeverdige.
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

    return None  # skal ikke inntreffe: target hentes fra result.checked selv


def fold_boundaries(result: TraceResult) -> BoundaryReport:
    """Steg 4 sin kjernefunksjon. O(noder + kanter), ingen I/O.

    Forutsetning: result kommer fra Tracer.trace()/trace_upstream() i steg 3
    (traverse.py) og er ikke mutert i mellomtiden.
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
            continue  # §5: ingen foreldre present -> ikke et funn her

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
    """Visningshjelp: oversett en reached_via-sti (UID-er) til kortnavn."""
    return tuple(
        result.checked[uid].name if uid in result.checked else uid.split(".")[-1]
        for uid in path
    )
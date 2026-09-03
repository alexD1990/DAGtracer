"""Steg 5 — sqlglot join-analyse (architectur.md §3 lag 4, §7 steg 1).

Ren fold over BoundaryFinding-er fra steg 4 (boundaries.py) + delt parset
SQL fra sqlparsing.py.

Bug fanget og rettet i steg 7-planleggingen (før noe skjema ble låst):
driving-side-hull. Opprinnelig kode lette kun etter fraværende foreldre
blant JOIN-nodenes MÅL (jn.this) — aldri blant FROM/driving-tabellen. En
fraværende forelder som er driving-siden (ikke joined-siden) ble dermed
aldri funnet, og konklusjonen ble feilaktig not_explained_by_join selv i
det klareste mulige tilfellet: mangler driving-tabellen raden, kan intet
join-oppsett bringe den tilbake. Ingen av de fem fasit-scenarioene i
testproject fanget dette (alle har den fraværende forelderen på joined-
siden), så det ble funnet ved gjennomlesing, ikke ved feilende test.
Kandidat for en sjette fasit-verdi i testproject — se docs/byggeplan.md.

Forklaringsregelen er IKKE symmetrisk mellom rollene (ikke forenkl bort
denne asymmetrien ved en senere refaktor):
  - role="driving", fraværende -> forklarer ved BÅDE inner og left, siden
    begge krever at driving-raden finnes for at noe skal produseres for
    den nøkkelen i det hele tatt.
  - role="joined", fraværende -> forklarer KUN ved inner (architectur.md
    §7 steg 1, ordrett: "LEFT JOIN der drivende side har verdien -> join
    kan ikke alene forklare bortfallet" — LEFT JOIN beholder driving-raden
    uansett hva som skjer på joined-siden).
  right/full/cross/semi/anti er fortsatt ikke semantisk modellert for
  NOEN rolle — flagges, explains_absence=False konservativt.

Fortsatt MVP-avgrensning, bevisst: kun foreldre som ER fraværende blir
analysert. En present forelder får aldri en JoinInfo (dvs. role="driving"
for en present driving-tabell vises ikke i output ennå) — det ble vurdert
og avgjort i steg 7-planleggingen å IKKE utvide nå, siden det kun er en
output-forbedring, ikke en korrekthetsfiks (i motsetning til hullet over).

Driving-tabell er fortsatt forenklet til "FROM-tabellen i samme
SELECT-scope". Dialekt er parameter, ikke konstant — dialekt-røyktest mot
Postgres er bevisst utsatt til beta.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlglot import exp

from .boundaries import BoundaryFinding, BoundaryReport, ParentStatus
from .metadata import lookup_node
from .project import DbtProject
from .sqlparsing import ParsedNode, parsed_sql_for_node

__all__ = [
    "CONCLUSION_JOIN_CONFIRMED",
    "CONCLUSION_NOT_EXPLAINED_BY_JOIN",
    "CONCLUSION_PARSE_UNCERTAIN",
    "JoinInfo",
    "JoinAnalysis",
    "analyze_join_for_finding",
    "analyze_joins",
]

CONCLUSION_JOIN_CONFIRMED = "join_confirmed"
CONCLUSION_NOT_EXPLAINED_BY_JOIN = "not_explained_by_join"
CONCLUSION_PARSE_UNCERTAIN = "parse_uncertain"

_DRIVING_EXPLAINS = ("inner", "left")
_JOINED_EXPLAINS = ("inner",)
_MODELED_JOIN_TYPES = ("inner", "left")


@dataclass(frozen=True)
class JoinInfo:
    parent_node_uid: str
    parent_name: str
    join_type: str
    role: str  # "driving" | "joined"
    condition: str | None
    explains_absence: bool
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class JoinAnalysis:
    node_uid: str
    name: str
    dialect: str
    parse_ok: bool
    joins: tuple[JoinInfo, ...]
    conclusion: str
    notes: tuple[str, ...] = ()


def _table_short_name(table_expr: exp.Expression | None) -> str | None:
    if isinstance(table_expr, exp.Table):
        return table_expr.name or None
    return None


def _driving_table_name(join_node: exp.Join) -> str | None:
    """Første tabell i FROM-klausulen til joinets nærmeste SELECT-scope.

    Nøkkelnavnet på FROM-klausulen i select.args varierer mellom
    sqlglot-versjoner ('from' vs. 'from_', bekreftet empirisk under steg
    6.5/orgnr 666 — se docs/byggeplan.md). Sjekker begge fremfor å anta én.
    """
    select = join_node.find_ancestor(exp.Select)
    if select is None:
        return None
    from_clause = select.args.get("from_") or select.args.get("from")
    if from_clause is None:
        return None
    table = from_clause.find(exp.Table)
    return _table_short_name(table)


def _join_kind(join_node: exp.Join) -> str:
    side = (join_node.args.get("side") or "").upper()
    kind = (join_node.args.get("kind") or "").upper()
    if kind == "CROSS":
        return "cross"
    if side == "LEFT":
        return "left"
    if side == "RIGHT":
        return "right"
    if side == "FULL":
        return "full"
    if kind == "SEMI":
        return "semi"
    if kind == "ANTI":
        return "anti"
    return "inner"


def _condition_text(join_node: exp.Join, dialect: str) -> str | None:
    on = join_node.args.get("on")
    if on is not None:
        return on.sql(dialect=dialect)
    using = join_node.args.get("using")
    if using:
        cols = ", ".join(u.sql(dialect=dialect) for u in using)
        return f"USING ({cols})"
    return None


def analyze_join_for_finding(
    finding: BoundaryFinding, parsed: ParsedNode, dialect: str
) -> JoinAnalysis:
    """Ren funksjon: tar imot et allerede parset tre (delt med filters.py
    via sqlparsing.parsed_sql_for_node), gjør ingen egen fil-lesing.
    """
    absent_parents: tuple[ParentStatus, ...] = finding.absent_parents

    if not absent_parents:
        return JoinAnalysis(
            node_uid=finding.node_uid, name=finding.name, dialect=dialect,
            parse_ok=True, joins=(), conclusion=CONCLUSION_NOT_EXPLAINED_BY_JOIN,
            notes=(
                "Ingen fraværende forelder å eliminere mot — alle direkte "
                "foreldre er present. Join kan derfor ikke forklare "
                "bortfallet her; se steg 6 (filtersjekk).",
            ),
        )

    if not parsed.ok or parsed.tree is None:
        return JoinAnalysis(
            node_uid=finding.node_uid, name=finding.name, dialect=dialect,
            parse_ok=False, joins=(), conclusion=CONCLUSION_PARSE_UNCERTAIN,
            notes=parsed.notes,
        )

    parent_by_short_name = {p.name.lower(): p for p in absent_parents}
    matched: dict[str, JoinInfo] = {}
    top_notes: list[str] = list(parsed.notes)

    for jn in parsed.tree.find_all(exp.Join):
        joined_name = _table_short_name(jn.this)
        driving_name = _driving_table_name(jn)
        kind = _join_kind(jn)
        condition = _condition_text(jn, dialect)

        same_table_both_roles = (
            joined_name is not None
            and driving_name is not None
            and joined_name.lower() == driving_name.lower()
        )

        for table_name, role, explains_kinds in (
            (driving_name, "driving", _DRIVING_EXPLAINS),
            (joined_name, "joined", _JOINED_EXPLAINS),
        ):
            if table_name is None:
                continue
            key = table_name.lower()
            parent = parent_by_short_name.get(key)
            if parent is None or key in matched:
                continue

            notes: list[str] = []
            explains = kind in explains_kinds
            if kind not in _MODELED_JOIN_TYPES:
                explains = False
                notes.append(
                    f"join-type '{kind}' er ikke semantisk modellert ennå "
                    "(kun inner/left er vurdert per architectur.md §7). "
                    "explains_absence er derfor konservativt satt til False."
                )
            if same_table_both_roles:
                explains = False
                notes.append(
                    f"'{table_name}' ser ut til å være både driving- og "
                    "join-tabell i samme SELECT (selv-join?). Rolletildeling "
                    "usikker — ikke stol på explains_absence for denne."
                )

            matched[key] = JoinInfo(
                parent_node_uid=parent.node_uid,
                parent_name=parent.name,
                join_type=kind,
                role=role,
                condition=condition,
                explains_absence=explains,
                notes=tuple(notes),
            )

    for key, parent in parent_by_short_name.items():
        if key not in matched:
            top_notes.append(
                f"{parent.name}: ikke funnet som JOIN-mål eller driving-tabell "
                "i kompilert SQL (kan være referert via CTE/subquery, eller "
                "ikke joinet i det hele tatt). Ingen join-basert forklaring "
                "gitt for denne forelderen."
            )

    conclusion = (
        CONCLUSION_JOIN_CONFIRMED
        if any(j.explains_absence for j in matched.values())
        else CONCLUSION_NOT_EXPLAINED_BY_JOIN
    )

    return JoinAnalysis(
        node_uid=finding.node_uid, name=finding.name, dialect=dialect,
        parse_ok=True, joins=tuple(matched.values()), conclusion=conclusion,
        notes=tuple(top_notes),
    )


def analyze_joins(
    report: BoundaryReport,
    manifest: dict[str, Any],
    *,
    project: DbtProject,
    dialect: str = "duckdb",
) -> tuple[JoinAnalysis, ...]:
    """Frittstående bekvemmelighetsfunksjon (brukt av step5_test.py alene).
    For delt parsing på tvers av join+filter i samme kjøring, se
    filters.diagnose_all().
    """
    results = []
    for finding in report.boundaries:
        node = lookup_node(manifest, finding.node_uid)
        parsed = parsed_sql_for_node(project, node, dialect)
        results.append(analyze_join_for_finding(finding, parsed, dialect))
    return tuple(results)
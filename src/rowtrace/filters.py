"""step 6 — filtersjekk and kombinert konklusjon (architectur.md §7 step 2-3).

To atskilte funksjoner, with hensikt: find_filters_for_tree() ser KUN on
noits own SQL, uavhengig by step 5 sitt resultat. diagnose() tar imot én
JoinAnalysis (step 5) and one FilterAnalysis (denne fila) and anvender ordrett
sannhetstabellen from architectur.md §7 step 3.

diagnose_all() parser kompilert SQL ÉN gang per grensenode (sqlparsing.py),
and uses same three for both join and filter analysis — replaces the
previous pattern where joins.py and filters.py parsed independently by
hverandre (funnet and rettet i step 7-planleggingen).

IMPORTANT LIMITATION (found during design of step 6, not in debugging):
find_filters_for_tree() ekskluderer eksplisitt WHERE/HAVING/QUALIFY that
ligger inne i en gren by a mengdeoperasjon (UNION/EXCEPT/INTERSECT).

Grunn: testproject/models/mart/mart_summary.sql (orgnr 555-scenarioet) has
en ekte "where orgnr = '555'" inni EXCEPT-ens andre gren. Uten denne
ekskluderingen ville find_filters_for_tree() funnet den klausulen and
rapportert filter_suspected — wrong by wrong grunn, siden arkitekturens
poeng with this scenarioet nettopp is at EXCEPT IKKE is et gjenkjent
pattern. The rule is general: set operations are not in the recognized
construction list in §7 step 2, so clauses that only exist to
konstruere en by grenene i en mengdeoperasjon should ikke telle that en
forklaring on noits EGEN filtrering.

Kjente begrensninger, deliberately utelatt from MVP:
  - RANK()/DENSE_RANK() gjenkjennes IKKE that dedup, kun ROW_NUMBER().
  - Dedup-alias tracking is only one alias level, does not follow multiple layers of
    CTE-videreforing.
  - references_key_column is navnematch, ikke semantisk column-lineage.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlglot import exp

from .boundaries import BoundaryFinding
from .joins import (
    CONCLUSION_JOIN_CONFIRMED,
    CONCLUSION_PARSE_UNCERTAIN,
    JoinAnalysis,
    analyze_join_for_finding,
)
from .metadata import lookup_node
from .project import DbtProject
from .sqlparsing import ParsedNode, parsed_sql_for_node

__all__ = [
    "CONCLUSION_FILTER_SUSPECTED",
    "CONCLUSION_UNEXPLAINED",
    "FilterFinding",
    "FilterAnalysis",
    "Diagnosis",
    "find_filters_for_tree",
    "find_filters",
    "diagnose",
    "diagnose_all",
]

CONCLUSION_FILTER_SUSPECTED = "filter_suspected"
CONCLUSION_UNEXPLAINED = "unexplained"

_SET_OP_TYPE_NAMES = ("Union", "Except", "Intersect")
_SET_OP_TYPES: tuple[type, ...] = tuple(
    getattr(exp, name) for name in _SET_OP_TYPE_NAMES if hasattr(exp, name)
)


@dataclass(frozen=True)
class FilterFinding:
    kind: str  # where|having|qualify|dedup_row_number|distinct_on
    clause: str
    references_key_column: bool


@dataclass(frozen=True)
class FilterAnalysis:
    node_uid: str
    name: str
    parse_ok: bool
    filters: tuple[FilterFinding, ...]
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class Diagnosis:
    """Endelig konklusjon (architectur.md §12.4-enumet) for one grensenode."""

    node_uid: str
    name: str
    conclusion: str  # join_confirmed|filter_suspected|unexplained|parse_uncertain
    join: JoinAnalysis
    filters: FilterAnalysis


def _inside_set_operation(node: exp.Expression) -> bool:
    if not _SET_OP_TYPES:
        return False
    return node.find_ancestor(*_SET_OP_TYPES) is not None


def _is_row_number_func(func: exp.Expression | None, dialect: str) -> bool:
    if func is None:
        return False
    row_number_cls = getattr(exp, "RowNumber", None)
    if row_number_cls is not None and isinstance(func, row_number_cls):
        return True
    try:
        return func.sql(dialect=dialect).strip().upper().startswith("ROW_NUMBER")
    except Exception:
        return False


def _window_alias_names(tree: exp.Expression, dialect: str) -> set[str]:
    names: set[str] = set()
    for win in tree.find_all(exp.Window):
        if not _is_row_number_func(win.this, dialect):
            continue
        alias_node = win.parent
        if isinstance(alias_node, exp.Alias):
            alias = alias_node.alias
            if alias:
                names.add(alias.lower())
    return names


def _is_dedup_condition(
    condition: exp.Expression, window_aliases: set[str], dialect: str
) -> bool:
    for win in condition.find_all(exp.Window):
        if _is_row_number_func(win.this, dialect):
            return True
    for col in condition.find_all(exp.Column):
        if col.name.lower() in window_aliases:
            return True
    return False


def _references_key_column(condition: exp.Expression, key_column: str) -> bool:
    key = key_column.lower()
    return any(col.name.lower() == key for col in condition.find_all(exp.Column))


def _distinct_on_findings(
    tree: exp.Expression, dialect: str, key_column: str
) -> list[FilterFinding]:
    findings: list[FilterFinding] = []
    for d in tree.find_all(exp.Distinct):
        on = d.args.get("on")
        if not on:
            continue
        cols = on.expressions if hasattr(on, "expressions") else [on]
        cols_sql = ", ".join(c.sql(dialect=dialect) for c in cols)
        references = any(
            isinstance(c, exp.Column) and c.name.lower() == key_column.lower() for c in cols
        )
        findings.append(
            FilterFinding(
                kind="distinct_on",
                clause=f"DISTINCT ON ({cols_sql})",
                references_key_column=references,
            )
        )
    return findings


def find_filters_for_tree(
    finding: BoundaryFinding, parsed: ParsedNode, key_column: str, dialect: str
) -> FilterAnalysis:
    """Ren funksjon: tar imot et allerede parset tre, delt with joins.py."""
    if not parsed.ok or parsed.tree is None:
        return FilterAnalysis(
            node_uid=finding.node_uid, name=finding.name, parse_ok=False,
            filters=(), notes=parsed.notes,
        )

    tree = parsed.tree
    window_aliases = _window_alias_names(tree, dialect)
    findings: list[FilterFinding] = []

    clause_types = (
        (exp.Where, "where"),
        (exp.Having, "having"),
        (exp.Qualify, "qualify"),
    )
    for cls, default_kind in clause_types:
        for clause_node in tree.find_all(cls):
            if _inside_set_operation(clause_node):
                continue  # see module doc: EXCEPT/UNION/INTERSECT-grener ekskludert
            condition = clause_node.this
            if condition is None:
                continue
            kind = (
                "dedup_row_number"
                if _is_dedup_condition(condition, window_aliases, dialect)
                else default_kind
            )
            findings.append(
                FilterFinding(
                    kind=kind,
                    clause=condition.sql(dialect=dialect),
                    references_key_column=_references_key_column(condition, key_column),
                )
            )

    findings.extend(_distinct_on_findings(tree, dialect, key_column))

    return FilterAnalysis(
        node_uid=finding.node_uid, name=finding.name, parse_ok=True,
        filters=tuple(findings), notes=parsed.notes,
    )


def find_filters(
    finding: BoundaryFinding,
    manifest: dict[str, Any],
    project: DbtProject,
    key_column: str,
    dialect: str,
) -> FilterAnalysis:
    """Standalone convenience function (isolated filter check). Parses
    itself. for delt parsing with joins.py i same run, se diagnose_all().
    """
    node = lookup_node(manifest, finding.node_uid)
    parsed = parsed_sql_for_node(project, node, dialect)
    return find_filters_for_tree(finding, parsed, key_column, dialect)


def diagnose(join: JoinAnalysis, filters: FilterAnalysis) -> Diagnosis:
    if join.conclusion == CONCLUSION_JOIN_CONFIRMED:
        conclusion = CONCLUSION_JOIN_CONFIRMED
    elif join.conclusion == CONCLUSION_PARSE_UNCERTAIN:
        conclusion = CONCLUSION_PARSE_UNCERTAIN
    elif not filters.parse_ok:
        conclusion = CONCLUSION_PARSE_UNCERTAIN
    elif filters.filters:
        conclusion = CONCLUSION_FILTER_SUSPECTED
    else:
        conclusion = CONCLUSION_UNEXPLAINED

    return Diagnosis(
        node_uid=join.node_uid, name=join.name, conclusion=conclusion,
        join=join, filters=filters,
    )


def diagnose_all(
    boundaries: tuple[BoundaryFinding, ...],
    manifest: dict[str, Any],
    project: DbtProject,
    key_column: str,
    *,
    dialect: str = "duckdb",
) -> tuple[Diagnosis, ...]:
    """step 7-planleggingens reviderte inngang: parser each grensenodes SQL
    ONCE, runs join and filter analysis against the same tree.
    """
    result: list[Diagnosis] = []
    for finding in boundaries:
        node = lookup_node(manifest, finding.node_uid)
        parsed = parsed_sql_for_node(project, node, dialect)
        join = analyze_join_for_finding(finding, parsed, dialect)
        filters = find_filters_for_tree(finding, parsed, key_column, dialect)
        result.append(diagnose(join, filters))
    return tuple(result)
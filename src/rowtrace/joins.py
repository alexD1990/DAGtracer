"""step 5 — sqlglot join analysis (architectur.md §3 layer 4, §7 step 1).

Pure fold over BoundaryFindings from step 4 (boundaries.py) + shared parsed
SQL from sqlparsing.py.

Bug caught and fixed in step 7 planning (before any schema was locked):
driving-side hole. Original code only looked for absent parents
among JOIN nodes' TARGETS (jn.this) — never among FROM/driving table. An
absent parent that is the driving side (not the joined side) was thus
never found, and the conclusion was incorrectly not_explained_by_join even in
the clearest possible case: if the driving table is missing the row, no
join setup can bring it back. None of the five reference scenarios in
testproject caught this (all had the absent parent on the joined
side), so it was found by review, not in a failing test.
Candidate for a sixth reference value in testproject — see docs/byggeplan.md.

The explanation rule is NOT symmetric between roles (don't simplify away
this asymmetry in a later refactor):
  - role="driving", absent -> explains BOTH inner and left, since
    both require the driving row to exist for anything to be produced for
    that key at all.
  - role="joined", absent -> explains ONLY inner (architectur.md
    §7 step 1, verbatim: "LEFT JOIN where driving side has the value -> join
    cannot alone explain the dropout" — LEFT JOIN retains the driving row
    regardless of what happens on the joined side).
  right/full/cross/semi/anti are still not semantically modeled for
  ANY role — flagged, explains_absence=False conservatively.

Still MVP limitation, deliberately: only parents that are absent are
analyzed. A present parent never gets a JoinInfo (i.e. role="driving"
for a present driving table is not shown in output yet) — this was considered
and decided in step 7 planning NOT to expand now, since it's only an
output improvement, not a correctness fix (unlike the hole above).

Driving table is still simplified to "the FROM table in the same
SELECT scope". Dialect is parameter, not constant — dialect smoke test against
Postgres is deliberately deferred to beta.
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
    """First table in the FROM clause of the join's nearest SELECT scope.

    The key name for the FROM clause in select.args varies between
    sqlglot versions ('from' vs. 'from_', confirmed empirically during step
    6.5/orgnr 666 — see docs/byggeplan.md). Checks both rather than assuming one.
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
    """Pure function: receives an already parsed tree (shared with filters.py
    via sqlparsing.parsed_sql_for_node), does no file reading itself.
    """
    absent_parents: tuple[ParentStatus, ...] = finding.absent_parents

    if not absent_parents:
        return JoinAnalysis(
            node_uid=finding.node_uid, name=finding.name, dialect=dialect,
            parse_ok=True, joins=(), conclusion=CONCLUSION_NOT_EXPLAINED_BY_JOIN,
            notes=(
                "No absent parent to eliminate against — all direct "
                "parents are present. Join therefore cannot explain "
                "the dropout here; see step 6 (filter check).",
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
                    f"join-type '{kind}' is not yet semantically modeled "
                    "(only inner/left are evaluated per architectur.md §7). "
                    "explains_absence is therefore conservatively set to False."
                )
            if same_table_both_roles:
                explains = False
                notes.append(
                    f"'{table_name}' appears to be both driving and "
                    "join table in same SELECT (self-join?). Role assignment "
                    "uncertain — don't trust explains_absence for this one."
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
                f"{parent.name}: not found as JOIN target or driving table "
                "in compiled SQL (may be referenced via CTE/subquery, or "
                "not joined at all). No join-based explanation "
                "given for this parent."
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
    """Standalone convenience function (used by step5_test.py alone).
    For shared parsing across join+filter in same run, see
    filters.diagnose_all().
    """
    results = []
    for finding in report.boundaries:
        node = lookup_node(manifest, finding.node_uid)
        parsed = parsed_sql_for_node(project, node, dialect)
        results.append(analyze_join_for_finding(finding, parsed, dialect))
    return tuple(results)
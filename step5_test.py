#!/usr/bin/env python3
"""Steg 5 manuell test: sqlglot join-analyse.

Dialekt-røyktest mot et ekte Postgres-prosjekt er bevisst UTSATT til beta
(docs/byggeplan.md, steg 5) — denne testen kjører kun mot testproject/DuckDB.

Kjør (fra repo-rot):
    python step5_test.py --project-dir testproject --profiles-dir testproject
    python step5_test.py --project-dir testproject --profiles-dir testproject --value 333
    python step5_test.py --project-dir testproject --profiles-dir testproject --value 555

Krever 'pip install sqlglot' i samme miljø som resten av verktøyet, hvis
det ikke allerede er installert.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from rowtrace.boundaries import fold_boundaries  # noqa: E402
from rowtrace.joins import (  # noqa: E402
    CONCLUSION_JOIN_CONFIRMED,
    CONCLUSION_NOT_EXPLAINED_BY_JOIN,
    analyze_joins,
)
from rowtrace.metadata import find_node, load_catalog, load_manifest  # noqa: E402
from rowtrace.project import DbtProject, RowtraceError  # noqa: E402
from rowtrace.traverse import trace_upstream  # noqa: E402

# Fasit: {barnnavn: forventet konklusjon}. Kun grensenoder som faktisk
# oppstår for hvert orgnr (jf. step4_test.py sin fasit) er med her.
#
# 222: prep_customer_orders er ENESTE grensenode. Fraværende forelder er
# stg_orders, joinet med INNER JOIN som non-driving side -> join_confirmed.
# Dette er brukerens opprinnelige manuelle testkriterium for steg 5.
#
# 333: to grensenoder (jf. step4_test.py).
#   - mart_summary: fraværende forelder er prep_active_orders, INNER JOIN
#     som non-driving side i CTE-en "combined" -> join_confirmed. (At det
#     EGENTLIG er et filter lenger opp i kjeden endrer ikke at joinet HER,
#     på mart_summary sitt eget nivå, strukturelt forklarer bortfallet.)
#   - prep_active_orders: ingen fraværende forelder i det hele tatt (dens
#     eneste forelder stg_orders er present) -> not_explained_by_join,
#     trivielt, uten SQL-parsing. Steg 6 skal finne WHERE-klausulen her.
#
# 555: mart_summary er eneste grensenode, begge foreldre present -> ingen
# fraværende forelder å eliminere mot -> not_explained_by_join, trivielt.
# Steg 6 skal gi unexplained (EXCEPT gjenkjennes ikke som filterkonstruksjon).
EXPECTED = {
    "222": {
        "prep_customer_orders": CONCLUSION_JOIN_CONFIRMED,
    },
    "333": {
        "mart_summary": CONCLUSION_JOIN_CONFIRMED,
        "prep_active_orders": CONCLUSION_NOT_EXPLAINED_BY_JOIN,
    },
    "555": {
        "mart_summary": CONCLUSION_NOT_EXPLAINED_BY_JOIN,
    },
    "666": {
        "mart_summary": CONCLUSION_JOIN_CONFIRMED,
        "prep_customer_orders": CONCLUSION_JOIN_CONFIRMED,
    },
}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--project-dir", required=True)
    p.add_argument("--profiles-dir", default=None)
    p.add_argument("--target", default=None)
    p.add_argument("--manifest", default=None)
    p.add_argument("--catalog", default=None)
    p.add_argument("--key", default="orgnr")
    p.add_argument("--value", default="222")
    p.add_argument("--from", dest="from_node", default="mart_summary")
    p.add_argument("--dialect", default="duckdb", help="sqlglot-dialekt for compiled_code")
    return p.parse_args()


def hdr(text: str) -> None:
    print(f"\n{text}\n{'-' * len(text)}")


def main() -> int:
    args = parse_args()

    project = DbtProject.create(
        project_dir=args.project_dir,
        profiles_dir=args.profiles_dir,
        target=args.target,
        manifest_path=args.manifest,
        catalog_path=args.catalog,
    )

    print(f"project_dir : {project.project_dir}")
    print(f"start       : {args.from_node}   {args.key}={args.value}")
    print(f"dialekt     : {args.dialect}")

    manifest = load_manifest(project)
    catalog = load_catalog(project, required=False)

    start_uid = find_node(manifest, args.from_node)

    hdr("Traversering + fold (steg 3-4, uendret)")
    result = trace_upstream(
        start_uid, args.key, args.value, project=project, manifest=manifest, catalog=catalog
    )
    report = fold_boundaries(result)
    print(f"  grensenoder: {[f.name for f in report.boundaries]}")

    hdr("Join-analyse (steg 5)")
    analyses = analyze_joins(report, manifest, project=project, dialect=args.dialect)

    got: dict[str, str] = {}
    for a in analyses:
        got[a.name] = a.conclusion
        print(f"\n  {a.name}  ->  {a.conclusion}  (parse_ok={a.parse_ok})")
        for j in a.joins:
            mark = "✅ forklarer" if j.explains_absence else "❌ forklarer ikke"
            print(f"      {j.parent_name:<24} {j.join_type:<6} {mark}")
            if j.condition:
                print(f"          on: {j.condition}")
            for n in j.notes:
                print(f"          note: {n}")
        for n in a.notes:
            print(f"      note: {n}")

    # --- Sammenlign mot fasit -------------------------------------------
    failures: list[str] = []
    expected = EXPECTED.get(args.value) if args.from_node == "mart_summary" else None

    hdr("Fasit-sammenligning")
    if expected is None:
        print(f"  Ingen fasit registrert for {args.from_node} / {args.value}.")
    else:
        if got != expected:
            failures.append(f"konklusjoner: forventet {expected}, fikk {got}")
        else:
            print("  Alt stemmer med fasit.")

    hdr("Oppsummering")
    if failures:
        for f in failures:
            print(f"  FEIL: {f}")
        print(f"\n{len(failures)} avvik.")
        return 1
    print("  OK")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RowtraceError as exc:
        print(f"\nVerktøyfeil: {exc}", file=sys.stderr)
        sys.exit(2)
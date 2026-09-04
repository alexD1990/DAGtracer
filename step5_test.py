#!/usr/bin/env python3
"""step 5 manual test: sqlglot join-analyse.

Dialekt-røyktest against a ekte Postgres-project is deliberately UTSATT to beta
(docs/byggeplan.md, step 5) — denne testen only runs against testproject/DuckDB.

Run (from repo root):
    python step5_test.py --project-dir testproject --profiles-dir testproject
    python step5_test.py --project-dir testproject --profiles-dir testproject --value 333
    python step5_test.py --project-dir testproject --profiles-dir testproject --value 555

Krever 'pip install sqlglot' in same miljø that resten by the tool, if
det ikke allerede is installert.
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

# Fasit: {barnnavn: forexpected konklusjon}. Kun grensenoder that faktisk
# oppstår for each orgnr (jf. step4_test.py sin fasit) is with her.
#
# 222: prep_customer_orders is ENESTE grensenode. Fraværende parent is
# stg_orders, joinet with INNER JOIN that non-driving side -> join_confirmed.
# This is brukerens opprinnelige manuelle testkriterium for step 5.
#
# 333: to grensenoder (jf. step4_test.py).
#   - mart_summary: fraværende parent is prep_active_orders, INNER JOIN
#     that non-driving side in CTE-a "combined" -> join_confirmed. (At det
#     EGENTLIG is a filter lenger opp in kjeden endrer not in joinet HER,
#     on mart_summary sitt eget nivå, strukturelt forklarer bortfallet.)
#   - prep_active_orders: no fraværende parent in det the whole tatt (dens
#     eneste parent stg_orders is present) -> not_explained_by_join,
#     trivielt, uten SQL-parsing. step 6 should finne WHERE-klausulen her.
#
# 555: mart_summary is eneste grensenode, begge foreldre present -> no
# fraværende parent å eliminere against -> not_explained_by_join, trivielt.
# step 6 should gi unexplained (EXCEPT gjenkjennes ikke that filterkonstruksjon).
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

    hdr("Traversering + fold (step 3-4, uendret)")
    result = trace_upstream(
        start_uid, args.key, args.value, project=project, manifest=manifest, catalog=catalog
    )
    report = fold_boundaries(result)
    print(f"  grensenoder: {[f.name for f in report.boundaries]}")

    hdr("Join-analyse (step 5)")
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

    # --- Sammenlign against reference -------------------------------------------
    failures: list[str] = []
    expected = EXPECTED.get(args.value) if args.from_node == "mart_summary" else None

    hdr("Fasit-sammenligning")
    if expected is None:
        print(f"  no fasit registrert for {args.from_node} / {args.value}.")
    else:
        if got != expected:
            failures.append(f"konklusjoner: forexpected {expected}, got {got}")
        else:
            print("  Everything matches reference.")

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
        print(f"\nTool error: {exc}", file=sys.stderr)
        sys.exit(2)
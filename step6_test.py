#!/usr/bin/env python3
"""Steg 6 manuell test: filtersjekk + kombinert konklusjon.

Kjør (fra repo-rot):
    python step6_test.py --project-dir testproject --profiles-dir testproject
    python step6_test.py --project-dir testproject --profiles-dir testproject --value 333
    python step6_test.py --project-dir testproject --profiles-dir testproject --value 555
    python step6_test.py --project-dir testproject --profiles-dir testproject --value 444 --from prep_deduped

OBS 444: forsvinner i prep_deduped, som ikke er en forelder av mart_summary
(jf. step4_test.py sin merknad). Må spores fra prep_deduped direkte for at
dedup-scenarioet faktisk skal testes.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from rowtrace.boundaries import fold_boundaries  # noqa: E402
from rowtrace.filters import diagnose_all  # noqa: E402
from rowtrace.metadata import find_node, load_catalog, load_manifest  # noqa: E402
from rowtrace.project import DbtProject, RowtraceError  # noqa: E402
from rowtrace.traverse import trace_upstream  # noqa: E402

# Fasit: {(from_node, value): {barnnavn: forventet endelig konklusjon}}
#
# 222: uendret fra steg 5 (join_confirmed alene, ingen filtersjekk trengs).
# 333: mart_summary uendret join_confirmed. prep_active_orders går fra
#      not_explained_by_join (steg 5) til filter_suspected (WHERE
#      status='ACTIVE' funnet).
# 555: mart_summary -> unexplained. Dette ER selve testen for
#      EXCEPT-avgrensningen i filters.py (se moduldoc der) — en feil her
#      betyr mest sannsynlig at settoperasjon-ekskluderingen ikke virker.
# 444 (fra prep_deduped, IKKE mart_summary): eneste grensenode er
#      prep_deduped selv, med dedup-mønsteret (underspørring row_number()
#      as rn, ytre where rn=1) -> filter_suspected.
EXPECTED = {
    ("mart_summary", "222"): {"prep_customer_orders": "join_confirmed"},
    ("mart_summary", "333"): {
        "mart_summary": "join_confirmed",
        "prep_active_orders": "filter_suspected",
    },
    ("mart_summary", "555"): {"mart_summary": "unexplained"},
    ("prep_deduped", "444"): {"prep_deduped": "filter_suspected"},
        ("mart_summary", "666"): {
        "mart_summary": "join_confirmed",
        "prep_customer_orders": "join_confirmed",
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
    p.add_argument("--dialect", default="duckdb")
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

    result = trace_upstream(
        start_uid, args.key, args.value, project=project, manifest=manifest, catalog=catalog
    )
    report = fold_boundaries(result)
    diagnoses = diagnose_all(
        report.boundaries, manifest, project, args.key, dialect=args.dialect
    )

    hdr("Diagnose (steg 5 + 6)")
    got: dict[str, str] = {}
    for d in diagnoses:
        got[d.name] = d.conclusion
        print(f"\n  {d.name}  ->  {d.conclusion}")
        if d.join.joins:
            for j in d.join.joins:
                mark = "✅" if j.explains_absence else "❌"
                print(f"      join   {j.parent_name:<24} {j.join_type:<6} role={j.role:<7} {mark}")
        if d.filters.filters:
            for f in d.filters.filters:
                ref = " (refererer nøkkelkolonne)" if f.references_key_column else ""
                print(f"      filter [{f.kind}] {f.clause}{ref}")
        for n in (*d.join.notes, *d.filters.notes):
            print(f"      note: {n}")

    # --- Sammenlign mot fasit -------------------------------------------
    failures: list[str] = []
    expected = EXPECTED.get((args.from_node, args.value))

    hdr("Fasit-sammenligning")
    if expected is None:
        print(f"  Ingen fasit registrert for {args.from_node} / {args.value}.")
    elif got != expected:
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
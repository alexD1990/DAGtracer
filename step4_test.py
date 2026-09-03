#!/usr/bin/env python3
"""Steg 4 manuell test: grensenode- og absent_at_source-identifisering.

Ren fold over TraceResult fra steg 3 (rowtrace.boundaries.fold_boundaries).
Ingen nye dbt-kall utover det trace_upstream (steg 3) uansett gjør.

Kjør (fra repo-rot):
    python step4_test.py --project-dir testproject --profiles-dir testproject
    python step4_test.py --project-dir testproject --profiles-dir testproject --value 333
    python step4_test.py --project-dir testproject --profiles-dir testproject --value 555

Fasiten under er utledet fra byggeplanens scenariotabell OG fra de faktiske
SQL-filene i testproject/models (ikke fra en tidligere kjøring), for de fem
orgnr fra mart_summary.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from rowtrace.boundaries import fold_boundaries, names_for_path  # noqa: E402
from rowtrace.metadata import find_node, load_catalog, load_manifest  # noqa: E402
from rowtrace.project import DbtProject, RowtraceError  # noqa: E402
from rowtrace.traverse import OUTCOME_COMPLETE, OUTCOME_START_PRESENT, trace_upstream  # noqa: E402

# Fasit for fold_boundaries() på trace fra mart_summary.
#
# "boundaries": {barnnavn: {forelderanvn: forventet_status, ...}, ...}
# "absent_at_source": {nodenavn, ...}
#
# Foreldre-strukturen (fra testproject/models/**/*.sql, ikke fra diagrammet
# i byggeplan.md, som er tvetydig på dette punktet):
#   prep_customer_orders <- stg_customers, stg_orders   (INNER JOIN)
#   prep_active_orders   <- stg_orders                  (kun én forelder!)
#   mart_summary         <- prep_customer_orders, prep_active_orders
EXPECTED = {
    # Kontroll: finnes overalt. Startnoden er present -> avbrutt umiddelbart,
    # ingen fold å gjøre (fold_boundaries kalles fortsatt, men edges er tomt).
    "111": {
        "outcome": OUTCOME_START_PRESENT,
        "boundaries": {},
        "absent_at_source": set(),
    },
    # Kunde uten ordre. prep_customer_orders mister raden pga INNER JOIN mot
    # stg_orders (absent), men stg_customers (present) gjør den til en
    # grensenode. prep_active_orders sin ENESTE forelder er stg_orders
    # (absent) -> ingen present forelder -> IKKE grensenode, traverseringen
    # fortsetter i stedet forbi den, via stg_orders, til raw_orders.
    "222": {
        "outcome": OUTCOME_COMPLETE,
        "boundaries": {
            "prep_customer_orders": {
                "stg_customers": "present",
                "stg_orders": "absent",
            },
        },
        "absent_at_source": {"raw_orders"},
    },
    # Filtrert bort i prep_active_orders (where status='ACTIVE'), men verdien
    # finnes hele veien til kilden. To grensenoder i samme kjede: selve
    # prep_active_orders (forelder stg_orders present, filter fjerner raden)
    # OG mart_summary ett hakk lenger ned (prep_customer_orders fortsatt
    # present der, siden den ikke filtrerer på status).
    "333": {
        "outcome": OUTCOME_COMPLETE,
        "boundaries": {
            "mart_summary": {
                "prep_customer_orders": "present",
                "prep_active_orders": "absent",
            },
            "prep_active_orders": {
                "stg_orders": "present",
            },
        },
        "absent_at_source": set(),
    },
    # OBS: 444 forsvinner i prep_deduped, som IKKE er en forelder av
    # mart_summary (prep_deduped er nedstrøms for prep_active_orders, en
    # søstergren mart_summary ikke bruker). Fra mart_summary er 444 derfor
    # present i startnoden -> samme utfall som kontrollverdien 111. Å faktisk
    # teste 444 sin grensenode krever --from prep_deduped, utenfor denne
    # fasiten (mart_summary er byggeplanens faste startpunkt for steg 4).
    "444": {
        "outcome": OUTCOME_START_PRESENT,
        "boundaries": {},
        "absent_at_source": set(),
    },
    # Forsvinner i mart_summary selv (EXCEPT-mønsteret), begge foreldre
    # present. Klassisk "ingen foreldre er skyld i det" - diagnostikklaget
    # i steg 6 skal gi unexplained her, men det er IKKE steg 4 sin jobb å
    # vite det; steg 4 skal bare se at mart_summary kvalifiserer strukturelt.
    "555": {
        "outcome": OUTCOME_COMPLETE,
        "boundaries": {
            "mart_summary": {
                "prep_customer_orders": "present",
                "prep_active_orders": "present",
            },
        },
        "absent_at_source": set(),
    },
    "666": {
        "outcome": OUTCOME_COMPLETE,
        "boundaries": {
            "mart_summary": {
                "prep_customer_orders": "absent",
                "prep_active_orders": "present",
            },
            "prep_customer_orders": {
                "stg_customers": "absent",
                "stg_orders": "present",
            },
        },
        "absent_at_source": {"raw_customers"},
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

    manifest = load_manifest(project)
    catalog = load_catalog(project, required=False)
    if catalog is None:
        print("\nADVARSEL: catalog.json mangler. Kjør 'dbt docs generate'.")

    start_uid = find_node(manifest, args.from_node)

    hdr("Traversering (steg 3, uendret)")
    result = trace_upstream(
        start_uid,
        args.key,
        args.value,
        project=project,
        manifest=manifest,
        catalog=catalog,
    )
    print(f"  utfall: {result.outcome}")
    print(f"  noder besøkt: {result.stats.nodes_visited}")

    hdr("Fold: grensenoder og absent_at_source (steg 4)")
    report = fold_boundaries(result)

    if not report.boundaries:
        print("  Ingen grensenoder.")
    for finding in report.boundaries:
        print(f"\n  {finding.name}  (dybde {finding.depth}, {finding.resource_type})")
        for parent in finding.parents:
            mark = "✅" if parent.present else "❌"
            print(f"      {mark} {parent.name:<24} {parent.status}")

    if report.absent_at_source:
        print()
        for finding in report.absent_at_source:
            path_names = " -> ".join(names_for_path(result, finding.reached_via))
            print(f"  ABSENT_AT_SOURCE: {finding.name}  (dybde {finding.depth})")
            print(f"      reached_via: {path_names}")
    else:
        print("\n  Ingen absent_at_source-funn.")

    # --- Sammenlign mot fasit -------------------------------------------
    failures: list[str] = []
    expected = EXPECTED.get(args.value) if args.from_node == "mart_summary" else None

    hdr("Fasit-sammenligning")
    if expected is None:
        print(f"  Ingen fasit registrert for {args.from_node} / {args.value}.")
    else:
        if result.outcome != expected["outcome"]:
            failures.append(
                f"utfall: forventet {expected['outcome']}, fikk {result.outcome}"
            )

        got_boundaries = {
            f.name: {p.name: p.status for p in f.parents} for f in report.boundaries
        }
        if got_boundaries != expected["boundaries"]:
            failures.append(
                f"boundaries: forventet {expected['boundaries']}, fikk {got_boundaries}"
            )

        got_aas = {f.name for f in report.absent_at_source}
        if got_aas != expected["absent_at_source"]:
            failures.append(
                f"absent_at_source: forventet {sorted(expected['absent_at_source'])}, "
                f"fikk {sorted(got_aas)}"
            )

        # reached_via strukturell sjekk (ikke eksakt sti - diamant kan gi
        # flere like korte, gyldige stier, jf. docstring i _shortest_path_uids).
        for finding in report.absent_at_source:
            path = finding.reached_via
            if path[0] != result.start_node or path[-1] != finding.node_uid:
                failures.append(
                    f"reached_via for {finding.name}: mangler start- eller sluttnode: {path}"
                )
            for a, b in zip(path, path[1:]):
                if b not in result.edges.get(a, []):
                    failures.append(
                        f"reached_via for {finding.name}: {a} -> {b} er ikke en reell kant"
                    )

        if not failures:
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
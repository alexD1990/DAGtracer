#!/usr/bin/env python3
"""step 4 manual test: grensenode- and absent_at_source-identifisering.

Ren fold over TraceResult from step 3 (rowtrace.boundaries.fold_boundaries).
no new dbt calls utover det trace_upstream (step 3) anyway does.

Run (from repo root):
    python step4_test.py --project-dir testproject --profiles-dir testproject
    python step4_test.py --project-dir testproject --profiles-dir testproject --value 333
    python step4_test.py --project-dir testproject --profiles-dir testproject --value 555

Fasiten under is derived from byggeplanens scenario table and from de actual
SQL files in testproject/models (ikke from a tidligere run), for the five
orgnr from mart_summary.
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

# Fasit for fold_boundaries() on trace from mart_summary.
#
# "boundaries": {barnnavn: {forelderanvn: forventet_status, ...}, ...}
# "absent_at_source": {nodenavn, ...}
#
# Foreldre-strukturen (from testproject/models/**/*.sql, not from diagrammet
# in byggeplan.md, that is tvetydig on dette punktet):
#   prep_customer_orders <- stg_customers, stg_orders   (INNER JOIN)
#   prep_active_orders   <- stg_orders                  (kun one parent!)
#   mart_summary         <- prep_customer_orders, prep_active_orders
EXPECTED = {
    # Kontroll: exists overalt. Startnoden is present -> avbrutt umiddelbart,
    # no fold to gjøre (fold_boundaries is called still, but edges are empty).
    "111": {
        "outcome": OUTCOME_START_PRESENT,
        "boundaries": {},
        "absent_at_source": set(),
    },
    # Customer without ordre. prep_customer_orders mister raden pga INNER JOIN against
    # stg_orders (absent), but stg_customers (present) makes it a a
    # grensenode. prep_active_orders sin ENESTE parent is stg_orders
    # (absent) -> no present parent -> IKKE grensenode, the traversal
    # fortsafter in instead forbi den, via stg_orders, to raw_orders.
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
    # Filtrert bort in prep_active_orders (where status='ACTIVE'), but the value
    # exists the whole veien to kilden. To grensenoder in same kjede: selve
    # prep_active_orders (parent stg_orders present, filter fjerner raden)
    # and mart_summary ett hakk lenger ned (prep_customer_orders still
    # present der, siden den ikke filtrerer on status).
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
    # OBS: 444 forswins in prep_deduped, that IKKE is a parent by
    # mart_summary (prep_deduped is downstream of prep_active_orders, a
    # søstergren mart_summary ikke bruker). from mart_summary is 444 derfor
    # present in startnoden -> same utfall that kontrollthe value 111. Å faktisk
    # teste 444 sin grensenode krever --from prep_deduped, outside denne
    # fasiten (mart_summary is byggeplanens faste startpunkt for step 4).
    "444": {
        "outcome": OUTCOME_START_PRESENT,
        "boundaries": {},
        "absent_at_source": set(),
    },
    # Forswins in mart_summary selv (EXCEPT-mønsteret), begge foreldre
    # present. Klassisk "no foreldre is skyld in det" - diagnostikklaget
    # in step 6 should gi unexplained her, but det is IKKE step 4 sin jobb å
    # vite det; step 4 should bare se at mart_summary kvalifiserer strukturelt.
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
        print("\nWARNING: catalog.json is missing. Run 'dbt docs generate'.")

    start_uid = find_node(manifest, args.from_node)

    hdr("Traversering (step 3, uendret)")
    result = trace_upstream(
        start_uid,
        args.key,
        args.value,
        project=project,
        manifest=manifest,
        catalog=catalog,
    )
    print(f"  utfall: {result.outcome}")
    print(f"  nodes besøkt: {result.stats.nodes_visited}")

    hdr("Fold: grensenoder and absent_at_source (step 4)")
    report = fold_boundaries(result)

    if not report.boundaries:
        print("  no grensenoder.")
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
        print("\n  no absent_at_source-funn.")

    # --- Sammenlign against reference -------------------------------------------
    failures: list[str] = []
    expected = EXPECTED.get(args.value) if args.from_node == "mart_summary" else None

    hdr("Fasit-sammenligning")
    if expected is None:
        print(f"  no fasit registrert for {args.from_node} / {args.value}.")
    else:
        if result.outcome != expected["outcome"]:
            failures.append(
                f"utfall: forexpected {expected['outcome']}, got {result.outcome}"
            )

        got_boundaries = {
            f.name: {p.name: p.status for p in f.parents} for f in report.boundaries
        }
        if got_boundaries != expected["boundaries"]:
            failures.append(
                f"boundaries: forexpected {expected['boundaries']}, got {got_boundaries}"
            )

        got_aas = {f.name for f in report.absent_at_source}
        if got_aas != expected["absent_at_source"]:
            failures.append(
                f"absent_at_source: forexpected {sorted(expected['absent_at_source'])}, "
                f"got {sorted(got_aas)}"
            )

        # reached_via strukturell sjekk (ikke eksakt sti - diamant can gi
        # multiple equally short, gyldige stier, jf. docstring in _shortest_path_uids).
        for finding in report.absent_at_source:
            path = finding.reached_via
            if path[0] != result.start_node or path[-1] != finding.node_uid:
                failures.append(
                    f"reached_via for {finding.name}: mangler start- or sluttnode: {path}"
                )
            for a, b in zip(path, path[1:]):
                if b not in result.edges.get(a, []):
                    failures.append(
                        f"reached_via for {finding.name}: {a} -> {b} is ikke a reell kant"
                    )

        if not failures:
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
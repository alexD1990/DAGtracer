#!/usr/bin/env python3
"""Steg 2 manuell test: foreldre-oppslag ett nivå + kolonnevalidering.

Beslutning E: prosjekt- og profilsti er parametere, aldri konstanter.

Kjør (fra repo-rot):
    python step2_test.py --project-dir testproject --profiles-dir testproject

Krever at 'dbt docs generate' er kjørt i prosjektet, slik at catalog.json
finnes. Uten --check gjøres ingen spørringer mot warehouse i det hele tatt.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from rowtrace.metadata import (  # noqa: E402
    CATALOG_COLUMN_MISSING,
    CATALOG_NODE_ABSENT,
    TraversalPolicy,
    classify_node,
    find_node,
    load_catalog,
    load_manifest,
    parents_of,
    resource_type_of,
)
from rowtrace.presence import check_presence_guarded  # noqa: E402
from rowtrace.project import DbtProject, RowtraceError  # noqa: E402

# Fasit fra docs/byggeplan.md. Kortnavn, siden testen er mot testproject.
EXPECTED_PARENTS = {
    "mart_summary": {"prep_customer_orders", "prep_active_orders"},
    "prep_customer_orders": {"stg_customers", "stg_orders"},
    "prep_active_orders": {"stg_orders"},
    "prep_deduped": {"prep_active_orders"},
    "stg_customers": {"raw_customers"},
    "stg_orders": {"raw_orders"},
}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--project-dir", required=True, help="dbt-prosjektet det testes mot")
    p.add_argument("--profiles-dir", default=None)
    p.add_argument("--target", default=None)
    p.add_argument("--manifest", default=None)
    p.add_argument("--catalog", default=None)
    p.add_argument("--key", default="orgnr", help="Nøkkelkolonne")
    p.add_argument("--value", default="222", help="Brukes kun sammen med --check")
    p.add_argument(
        "--from",
        dest="from_node",
        default="mart_summary",
        help="Startnode for hovedtesten",
    )
    p.add_argument(
        "--no-seeds", action="store_true", help="Slå av seed-traversering (policytest)"
    )
    p.add_argument(
        "--no-sources", action="store_true", help="Slå av source-traversering"
    )
    p.add_argument(
        "--check",
        action="store_true",
        help="Kjør faktiske presence-sjekker på foreldrene (~2,75 s per kall)",
    )
    return p.parse_args()


def hdr(text: str) -> None:
    print(f"\n{text}\n{'-' * len(text)}")


def fmt_ref(ref) -> str:
    flags = []
    if ref.checkable:
        flags.append("checkable")
    if ref.traverse_through:
        flags.append("traverse")
    if ref.terminal:
        flags.append("terminal")
    if ref.skip_reason:
        flags.append(f"skip={ref.skip_reason}")
    if ref.key_column_type:
        flags.append(f"type={ref.key_column_type}")
    if ref.catalog_status in (CATALOG_NODE_ABSENT, CATALOG_COLUMN_MISSING):
        flags.append(f"catalog={ref.catalog_status}")
    return f"  {ref.name:<24} {ref.resource_type:<9} [{', '.join(flags)}]"


def main() -> int:
    args = parse_args()

    project = DbtProject.create(
        project_dir=args.project_dir,
        profiles_dir=args.profiles_dir,
        target=args.target,
        manifest_path=args.manifest,
        catalog_path=args.catalog,
    )
    policy = TraversalPolicy(
        include_seeds=not args.no_seeds,
        include_sources=not args.no_sources,
    )

    print(f"project_dir : {project.project_dir}")
    print(f"profiles_dir: {project.profiles_dir or '(dbt default)'}")
    print(f"manifest    : {project.manifest_file}")
    print(f"catalog     : {project.catalog_file}")
    print(f"policy      : seeds={policy.include_seeds} sources={policy.include_sources}")

    manifest = load_manifest(project)
    catalog = load_catalog(project, required=False)
    if catalog is None:
        print(
            "\nADVARSEL: catalog.json finnes ikke. Kolonnevalidering (§4) blir "
            "hoppet over.\n         Kjør 'dbt docs generate' i prosjektet og "
            "prøv igjen.",
        )

    failures: list[str] = []

    # --- Hovedtest: mart_summary skal gi begge foreldre -------------------
    hdr(f"Hovedtest: foreldre til '{args.from_node}'")
    start_uid = find_node(manifest, args.from_node)
    print(f"UID: {start_uid}")
    parents = parents_of(manifest, catalog, start_uid, args.key, policy)
    for ref in parents:
        print(fmt_ref(ref))

    got = {r.name for r in parents}
    want = EXPECTED_PARENTS.get(args.from_node)
    if want is None:
        print(f"  (ingen fasit registrert for '{args.from_node}')")
    elif got == want:
        print(f"  OK: fikk forventede foreldre {sorted(want)}")
    else:
        msg = f"{args.from_node}: forventet {sorted(want)}, fikk {sorted(got)}"
        failures.append(msg)
        print(f"  FEIL: {msg}")

    # --- Hele fasit-grafen, ett nivå per node ----------------------------
    hdr("Foreldre for alle noder i fasit-tabellen")
    for name, want in EXPECTED_PARENTS.items():
        try:
            uid = find_node(manifest, name)
        except RowtraceError as exc:
            failures.append(f"{name}: {exc}")
            print(f"{name}: FEIL - {exc}")
            continue
        refs = parents_of(manifest, catalog, uid, args.key, policy)
        got = {r.name for r in refs}
        ok = got == want
        print(f"{name} -> {sorted(got) or '[]'}  {'OK' if ok else 'AVVIK'}")
        for ref in refs:
            print(fmt_ref(ref))
        if not ok:
            failures.append(f"{name}: forventet {sorted(want)}, fikk {sorted(got)}")

    # --- Modellgrensen: stg_customers sin forelder er en seed ------------
    hdr("Policytest: modellgrensen mot seeds")
    stg_uid = find_node(manifest, "stg_customers")
    seed_parents = parents_of(manifest, catalog, stg_uid, args.key, policy)
    for ref in seed_parents:
        rt = resource_type_of(ref.node_uid)
        expect_checkable = policy.include_seeds and rt == "seed"
        state = "sjekkes" if ref.checkable else f"hoppes over ({ref.skip_reason})"
        print(f"  {ref.node_uid}  ->  {state}")
        if rt == "seed" and ref.checkable != expect_checkable:
            failures.append(
                f"seed-policy: {ref.name} checkable={ref.checkable}, "
                f"forventet {expect_checkable}"
            )

    # --- Kolonnevalidering ------------------------------------------------
    hdr(f"Kolonnevalidering: '{args.key}' via catalog.json")
    for name in EXPECTED_PARENTS:
        uid = find_node(manifest, name)
        ref = classify_node(manifest, catalog, uid, args.key, policy)
        print(
            f"  {ref.name:<24} catalog={ref.catalog_status:<14} "
            f"type={ref.key_column_type or '-':<10} checkable={ref.checkable}"
        )
        if catalog is not None and ref.catalog_status == CATALOG_COLUMN_MISSING:
            failures.append(f"{name}: '{args.key}' mangler i catalog — uventet i fasit")

    hdr("Negativ kontroll: ukjent kolonne skal gi missing_key_column")
    bogus = "kolonne_som_ikke_finnes"
    ref = classify_node(manifest, catalog, start_uid, bogus, policy)
    if catalog is None:
        print("  Hoppet over (ingen catalog lastet).")
    elif ref.skip_reason == "missing_key_column" and not ref.checkable:
        print(f"  OK: {args.from_node} hoppes over for '{bogus}'")
    else:
        msg = f"forventet missing_key_column for '{bogus}', fikk {ref.skip_reason!r}"
        failures.append(msg)
        print(f"  FEIL: {msg}")

    # --- Valgfrie faktiske presence-sjekker ------------------------------
    if args.check:
        hdr(f"Presence-sjekk av foreldrene til {args.from_node} ({args.key}={args.value})")
        for ref in parents:
            res = check_presence_guarded(
                ref.node_uid,
                args.key,
                args.value,
                project=project,
                manifest=manifest,
                catalog=catalog,
                policy=policy,
            )
            secs = f"{res.elapsed_s:.2f}s" if res.elapsed_s else "-"
            print(f"  {res.name:<24} {res.status:<28} n={res.count} {secs}")
            if res.detail:
                print(f"      detalj: {res.detail}")

    hdr("Oppsummering")
    if failures:
        for f in failures:
            print(f"  FEIL: {f}")
        print(f"\n{len(failures)} avvik.")
        return 1
    print("  Alt stemmer med fasit.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
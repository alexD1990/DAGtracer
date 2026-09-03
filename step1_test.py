"""Steg 1: verifiser check_presence() mot kjent fasit i testproject/.

Kjør fra repo-roten:
    python step1_test.py
    python step1_test.py --project-dir testproject --profiles-dir testproject
    python step1_test.py --orgnr 222 --quote-style plain --show-sql

Skriver ingenting og endrer ingenting - kjører kun count-spørringer.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from rowtrace.presence import (  # noqa: E402
    DbtProject,
    PresenceError,
    check_presence_detailed,
    find_model,
    load_manifest,
)

MODELS = [
    "stg_customers",
    "stg_orders",
    "prep_customer_orders",
    "prep_active_orders",
    "prep_deduped",
    "mart_summary",
]

# Fasit utledet fra seeds/raw_*.csv og models/**/*.sql.
# True = verdien skal finnes i modellen.
FASIT: dict[str, dict[str, bool]] = {
    # Kontrollverdi: ordre 1001 er ACTIVE og eneste rad for order_id 1001.
    "111": {m: True for m in MODELS},
    # Kunde uten ordre: faller ut i INNER JOIN, finnes aldri i ordrekjeden.
    "222": {
        "stg_customers": True,
        "stg_orders": False,
        "prep_customer_orders": False,
        "prep_active_orders": False,
        "prep_deduped": False,
        "mart_summary": False,
    },
    # CANCELLED-ordre: filtreres bort av where status = 'ACTIVE'.
    "333": {
        "stg_customers": True,
        "stg_orders": True,
        "prep_customer_orders": True,
        "prep_active_orders": False,
        "prep_deduped": False,
        "mart_summary": False,
    },
    # Taper dedup på order_id 1002 mot 555, men finnes fortsatt i mart_summary.
    "444": {
        "stg_customers": True,
        "stg_orders": True,
        "prep_customer_orders": True,
        "prep_active_orders": True,
        "prep_deduped": False,
        "mart_summary": True,
    },
    # Finnes overalt oppstrøms; fjernes av EXCEPT i mart_summary selv.
    "555": {
        "stg_customers": True,
        "stg_orders": True,
        "prep_customer_orders": True,
        "prep_active_orders": True,
        "prep_deduped": True,
        "mart_summary": False,
    },
}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Steg 1 - presence-sjekk mot fasit.")
    # Beslutning E: stier er parametere, med testprosjektet kun som default.
    p.add_argument("--project-dir", default="testproject")
    p.add_argument("--profiles-dir", default="testproject")
    p.add_argument("--target", default=None)
    p.add_argument("--manifest", default=None)
    p.add_argument("--key", default="orgnr")
    p.add_argument(
        "--orgnr",
        action="append",
        help="Kjør kun disse verdiene (kan gjentas). Standard: alle fem.",
    )
    p.add_argument("--model", action="append", help="Kjør kun disse modellene.")
    p.add_argument("--quote-style", choices=["adapter", "plain"], default="adapter")
    p.add_argument("--show-sql", action="store_true", help="Skriv generert SQL per kall.")
    return p.parse_args()


def main() -> int:
    args = parse_args()

    try:
        project = DbtProject.create(
            args.project_dir, args.profiles_dir, args.target, args.manifest
        )
        manifest = load_manifest(project)
    except PresenceError as exc:
        print(f"OPPSETTSFEIL: {exc}", file=sys.stderr)
        return 2

    values = args.orgnr or sorted(FASIT)
    models = args.model or MODELS

    print(f"prosjekt : {project.project_dir}")
    print(f"profiler : {project.profiles_dir or '(dbt default)'}")
    print(f"target   : {project.target or '(profilens default)'}")
    print(f"manifest : {project.manifest_file}")
    print(f"nøkkel   : {args.key}   quoting: {args.quote_style}\n")

    try:
        uids = {name: find_model(manifest, name) for name in models}
    except PresenceError as exc:
        print(f"OPPSETTSFEIL: {exc}", file=sys.stderr)
        return 2

    header = f"{'verdi':<7}{'modell':<24}{'faktisk':<10}{'fasit':<10}{'':<6}{'sek':>7}"
    print(header)
    print("-" * len(header))

    failures: list[str] = []
    errors: list[str] = []
    timings: list[float] = []
    wall_start = time.perf_counter()

    for value in values:
        expected_row = FASIT.get(value, {})
        for name in models:
            try:
                res = check_presence_detailed(
                    uids[name],
                    args.key,
                    value,
                    project=project,
                    manifest=manifest,
                    quote_style=args.quote_style,
                )
            except PresenceError as exc:
                errors.append(f"{value}/{name}: {exc}")
                print(f"{value:<7}{name:<24}{'FEIL':<10}{'':<10}{'💥':<6}{'-':>7}")
                continue

            timings.append(res.elapsed_s)
            expected = expected_row.get(name)
            actual_txt = "present" if res.present else "absent"
            if expected is None:
                mark, expected_txt = "?", "ukjent"
            elif res.present == expected:
                mark, expected_txt = "✅", "present" if expected else "absent"
            else:
                mark, expected_txt = "❌", "present" if expected else "absent"
                failures.append(
                    f"{value}/{name}: fikk {actual_txt} (count={res.count}), ventet {expected_txt}"
                )

            print(
                f"{value:<7}{name:<24}{actual_txt:<10}{expected_txt:<10}"
                f"{mark:<6}{res.elapsed_s:>7.2f}"
            )
            if args.show_sql:
                print(f"        relasjon: {res.relation}")
                print(f"        sql     : {res.sql}")

    wall = time.perf_counter() - wall_start
    print()
    if timings:
        # Dette tallet avgjør om subprocess holder til steg 3, jf. byggeplanen.
        print(
            f"{len(timings)} kall | snitt {sum(timings)/len(timings):.2f}s | "
            f"min {min(timings):.2f}s | maks {max(timings):.2f}s | totalt {wall:.1f}s"
        )
        est = (sum(timings) / len(timings)) * 150
        print(f"Ekstrapolert til 150 noder uten pruning/cache: ~{est/60:.1f} min.")

    for msg in errors:
        print(f"FEIL   {msg}")
    for msg in failures:
        print(f"AVVIK  {msg}")

    if errors:
        return 2
    if failures:
        print(f"\n{len(failures)} avvik mot fasit.")
        return 1
    print("\nAlt stemmer med fasit.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
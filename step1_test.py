"""step 1: verify check_presence() against known reference in testproject/.

Run from repo root:
    python step1_test.py
    python step1_test.py --project-dir testproject --profiles-dir testproject
    python step1_test.py --orgnr 222 --quote-style plain --show-sql

Writes nothing and changes nothing - only runs count queries.
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

# Fasit derived from seeds/raw_*.csv and models/**/*.sql.
# True = the value should exists in the model.
FASIT: dict[str, dict[str, bool]] = {
    # Control value: order 1001 is ACTIVE and only row for order_id 1001.
    "111": {m: True for m in MODELS},
    # Customer without ordre: faller ut in INNER JOIN, exists aldri in order chain.
    "222": {
        "stg_customers": True,
        "stg_orders": False,
        "prep_customer_orders": False,
        "prep_active_orders": False,
        "prep_deduped": False,
        "mart_summary": False,
    },
    # CANCELLED order: filtered out by where status = 'ACTIVE'.
    "333": {
        "stg_customers": True,
        "stg_orders": True,
        "prep_customer_orders": True,
        "prep_active_orders": False,
        "prep_deduped": False,
        "mart_summary": False,
    },
    # Loses dedup on order_id on order_id 1002 against against 555, but still exists in mart_summary.
    "444": {
        "stg_customers": True,
        "stg_orders": True,
        "prep_customer_orders": True,
        "prep_active_orders": True,
        "prep_deduped": False,
        "mart_summary": True,
    },
    # Exists everywhere upstream; removed by EXCEPT in mart_summary itself.
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
    p = argparse.ArgumentParser(description="step 1 - presence check against reference.")
    # Decision E: paths are parameters, with test project only as default.
    p.add_argument("--project-dir", default="testproject")
    p.add_argument("--profiles-dir", default="testproject")
    p.add_argument("--target", default=None)
    p.add_argument("--manifest", default=None)
    p.add_argument("--key", default="orgnr")
    p.add_argument(
        "--orgnr",
        action="append",
        help="Run only these the values (can repeated). Default: all five.",
    )
    p.add_argument("--model", action="append", help="Run only these models.")
    p.add_argument("--quote-style", choices=["adapter", "plain"], default="adapter")
    p.add_argument("--show-sql", action="store_true", help="Print generated SQL per call.")
    return p.parse_args()


def main() -> int:
    args = parse_args()

    try:
        project = DbtProject.create(
            args.project_dir, args.profiles_dir, args.target, args.manifest
        )
        manifest = load_manifest(project)
    except PresenceError as exc:
        print(f"SETUP ERROR: {exc}", file=sys.stderr)
        return 2

    values = args.orgnr or sorted(FASIT)
    models = args.model or MODELS

    print(f"project : {project.project_dir}")
    print(f"profiler : {project.profiles_dir or '(dbt default)'}")
    print(f"target   : {project.target or '(profile default)'}")
    print(f"manifest : {project.manifest_file}")
    print(f"key     : {args.key}   quoting: {args.quote_style}\n")

    try:
        uids = {name: find_model(manifest, name) for name in models}
    except PresenceError as exc:
        print(f"SETUP ERROR: {exc}", file=sys.stderr)
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
                mark, expected_txt = "?", "unknown"
            elif res.present == expected:
                mark, expected_txt = "✅", "present" if expected else "absent"
            else:
                mark, expected_txt = "❌", "present" if expected else "absent"
                failures.append(
                    f"{value}/{name}: got {actual_txt} (count={res.count}), expected {expected_txt}"
                )

            print(
                f"{value:<7}{name:<24}{actual_txt:<10}{expected_txt:<10}"
                f"{mark:<6}{res.elapsed_s:>7.2f}"
            )
            if args.show_sql:
                print(f"        relation: {res.relation}")
                print(f"        sql     : {res.sql}")

    wall = time.perf_counter() - wall_start
    print()
    if timings:
        # This number determines whether subprocess holds to step 3, cf. byggeplan.
        print(
            f"{len(timings)} calls | average {sum(timings)/len(timings):.2f}s | "
            f"min {min(timings):.2f}s | max {max(timings):.2f}s | total {wall:.1f}s"
        )
        est = (sum(timings) / len(timings)) * 150
        print(f"Extrapolated to 150 nodes without pruning/cache: ~{est/60:.1f} min.")

    for msg in errors:
        print(f"ERROR   {msg}")
    for msg in failures:
        print(f"MISMATCH  {msg}")

    if errors:
        return 2
    if failures:
        print(f"\n{len(failures)} mismatch against reference.")
        return 1
    print("\nEverything matches reference.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
#!/usr/bin/env python3
"""Steg 7 manuell test: bygg og valider det komplette JSON-dokumentet.

Kjør:
    python step7_test.py --project-dir testproject --profiles-dir testproject --value 222
    python step7_test.py --project-dir testproject --profiles-dir testproject --value 666
    python step7_test.py --project-dir testproject --profiles-dir testproject --value 111
    python step7_test.py --project-dir testproject --profiles-dir testproject --value 222 --print

Sjekker KUN det som er nytt i steg 7 (schema_version, status, join.role i
det serialiserte dokumentet) — selve konklusjonene er allerede fasit-testet
i step4/5/6_test.py, ikke dupliser det her.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from rowtrace import __version__  # noqa: E402
from rowtrace.boundaries import fold_boundaries  # noqa: E402
from rowtrace.filters import diagnose_all  # noqa: E402
from rowtrace.metadata import find_node, load_catalog, load_manifest  # noqa: E402
from rowtrace.output import build_report  # noqa: E402
from rowtrace.project import DbtProject, RowtraceError  # noqa: E402
from rowtrace.traverse import trace_upstream  # noqa: E402

EXPECTED = {
    "222": {
        "status": "complete",
        "boundary_roles": {"prep_customer_orders": {"stg_orders": "joined"}},
    },
    "666": {
        "status": "complete",
        "boundary_roles": {
            "mart_summary": {"prep_customer_orders": "driving"},
            "prep_customer_orders": {"stg_customers": "driving"},
        },
    },
    "111": {"status": "complete", "boundary_roles": {}},
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
    p.add_argument("--include-checked", action="store_true")
    p.add_argument("--print", action="store_true", help="Skriv ut hele JSON-dokumentet")
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

    manifest = load_manifest(project)
    catalog = load_catalog(project, required=False)
    start_uid = find_node(manifest, args.from_node)

    started_at = datetime.now(timezone.utc).isoformat()
    result = trace_upstream(
        start_uid, args.key, args.value, project=project, manifest=manifest, catalog=catalog
    )
    report = fold_boundaries(result)
    diagnoses = diagnose_all(report.boundaries, manifest, project, args.key, dialect=args.dialect)
    finished_at = datetime.now(timezone.utc).isoformat()

    doc = build_report(
        result, report, diagnoses,
        tool_version=__version__,
        started_at=started_at,
        finished_at=finished_at,
        target=args.target,
        dialect=args.dialect,
        include_checked=args.include_checked,
    )

    hdr("JSON-serialisering")
    try:
        text = json.dumps(doc, indent=2, ensure_ascii=False)
    except TypeError as exc:
        print(f"  FEIL: dokumentet er ikke JSON-serialiserbart: {exc}")
        return 1
    print(f"  OK, {len(text)} tegn.")
    if args.print:
        print(text)

    failures: list[str] = []
    expected = EXPECTED.get(args.value) if args.from_node == "mart_summary" else None

    hdr("Fasit-sammenligning (kun steg 7-spesifikke felt)")
    if expected is None:
        print(f"  Ingen fasit registrert for {args.from_node} / {args.value}.")
    else:
        if doc["schema_version"] != "1.0":
            failures.append(f"schema_version: forventet '1.0', fikk {doc['schema_version']!r}")
        if doc["status"] != expected["status"]:
            failures.append(f"status: forventet {expected['status']!r}, fikk {doc['status']!r}")

        got_roles: dict[str, dict[str, str]] = {}
        for b in doc["boundaries"]:
            roles = {p["name"]: p["join"]["role"] for p in b["parents"] if p["join"] is not None}
            if roles:
                got_roles[b["name"]] = roles

        if got_roles != expected["boundary_roles"]:
            failures.append(
                f"boundary_roles: forventet {expected['boundary_roles']}, fikk {got_roles}"
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
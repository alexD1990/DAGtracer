#!/usr/bin/env python3
"""Steg 8 manuell test: den faktiske CLI-en, kjørt som subprocess (mest
realistiske testen - dette ER grensesnittet brukeren faktisk vil bruke).

Sjekker exit-koder og grunnleggende output-form for et representativt
utvalg: kontrollverdi (exit 0), et reelt funn (exit 1, json og text),
ndjson-strømmen, --fail-on-tilpasning, og de to ulike feilveiene som gir
exit 2 (RowtraceError fra cli.py sin egen try/except, og
status='failed'-veien fra steg 7/traverse.py sitt outcome-enum).

Kjør:
    python step8_test.py --project-dir testproject --profiles-dir testproject
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--project-dir", required=True)
    p.add_argument("--profiles-dir", default=None)
    return p.parse_args()


def hdr(text: str) -> None:
    print(f"\n{text}\n{'-' * len(text)}")


def run_cli(project_dir: str, profiles_dir: str | None, *extra: str) -> subprocess.CompletedProcess:
    cmd = [sys.executable, "dbt_lineage.py", "trace", "--project-dir", project_dir]
    if profiles_dir:
        cmd += ["--profiles-dir", profiles_dir]
    cmd += list(extra)
    return subprocess.run(cmd, cwd=str(REPO_ROOT), capture_output=True, text=True)


def main() -> int:
    args = parse_args()
    failures: list[str] = []

    def check(label: str, condition: bool, detail: str = "") -> None:
        status = "OK" if condition else "FEIL"
        print(f"  [{status}] {label}" + (f" — {detail}" if detail and not condition else ""))
        if not condition:
            failures.append(label)

    hdr("1) Kontrollverdi 111 --output json -> exit 0")
    r = run_cli(args.project_dir, args.profiles_dir, "--key", "orgnr", "--value", "111",
                "--from", "mart_summary", "--output", "json")
    check("exit code == 0", r.returncode == 0, f"fikk {r.returncode}")
    try:
        doc = json.loads(r.stdout)
        check("gyldig JSON", True)
        check("status == complete", doc.get("status") == "complete", doc.get("status"))
        check("ingen boundaries", doc.get("boundaries") == [], doc.get("boundaries"))
    except json.JSONDecodeError as exc:
        check("gyldig JSON", False, str(exc))

    hdr("2) 222 --output json -> exit 1, join_confirmed")
    r = run_cli(args.project_dir, args.profiles_dir, "--key", "orgnr", "--value", "222",
                "--from", "mart_summary", "--output", "json")
    check("exit code == 1", r.returncode == 1, f"fikk {r.returncode}")
    try:
        doc = json.loads(r.stdout)
        conclusions = {b["name"]: b["conclusion"] for b in doc.get("boundaries", [])}
        check(
            "prep_customer_orders -> join_confirmed",
            conclusions.get("prep_customer_orders") == "join_confirmed",
            conclusions,
        )
    except json.JSONDecodeError as exc:
        check("gyldig JSON", False, str(exc))

    hdr("3) 222 --output text -> exit 1, lesbar tekst")
    r = run_cli(args.project_dir, args.profiles_dir, "--key", "orgnr", "--value", "222",
                "--from", "mart_summary", "--output", "text")
    check("exit code == 1", r.returncode == 1, f"fikk {r.returncode}")
    check("inneholder 'join_confirmed'", "join_confirmed" in r.stdout)
    check("inneholder 'prep_customer_orders'", "prep_customer_orders" in r.stdout)

    hdr("4) 666 --output ndjson -> gyldige linjer, siste er run_finished")
    r = run_cli(args.project_dir, args.profiles_dir, "--key", "orgnr", "--value", "666",
                "--from", "mart_summary", "--output", "ndjson")
    check("exit code == 1", r.returncode == 1, f"fikk {r.returncode}")
    lines = [ln for ln in r.stdout.splitlines() if ln.strip()]
    all_valid = True
    events = []
    for ln in lines:
        try:
            events.append(json.loads(ln))
        except json.JSONDecodeError:
            all_valid = False
    check("alle linjer er gyldig JSON", all_valid, f"{len(lines)} linjer")
    check("siste hendelse er run_finished", bool(events) and events[-1].get("event") == "run_finished")
    check(
        "minst én finding-hendelse med kind=boundary",
        any(e.get("event") == "finding" and e.get("kind") == "boundary" for e in events),
    )

    hdr("5) 222 --fail-on '' (tom) -> exit 0 selv med funn")
    r = run_cli(args.project_dir, args.profiles_dir, "--key", "orgnr", "--value", "222",
                "--from", "mart_summary", "--output", "json", "--fail-on", "")
    check("exit code == 0", r.returncode == 0, f"fikk {r.returncode}")

    hdr("6) Ukjent --key -> exit 2 (status=failed-veien, ikke RowtraceError)")
    r = run_cli(args.project_dir, args.profiles_dir, "--key", "finnes_ikke", "--value", "222",
                "--from", "mart_summary", "--output", "json")
    check("exit code == 2", r.returncode == 2, f"fikk {r.returncode}, stderr={r.stderr[:200]!r}")

    hdr("7) Ukjent --from -> exit 2 (RowtraceError-veien i cli.py)")
    r = run_cli(args.project_dir, args.profiles_dir, "--key", "orgnr", "--value", "222",
                "--from", "modell_som_ikke_finnes")
    check("exit code == 2", r.returncode == 2, f"fikk {r.returncode}")
    check("feilmelding på stderr", "Verktøyfeil" in r.stderr, r.stderr[:200])

    hdr("Oppsummering")
    if failures:
        for f in failures:
            print(f"  FEIL: {f}")
        print(f"\n{len(failures)} avvik.")
        return 1
    print("  OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
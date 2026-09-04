#!/usr/bin/env python3
"""step 3 manual test: rekursiv traversering with pruning and cache.

Decision E: project- and profilsti is parametere.

Run (from repo root):
    python step3_test.py --project-dir testproject --profiles-dir testproject
    python step3_test.py --project-dir testproject --profiles-dir testproject --value 111
    python step3_test.py --project-dir testproject --profiles-dir testproject --no-cache

Each run costs ~2,75 s per unique node. Full traversal for 222 is
seks kall, thus around 17 s.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from rowtrace.metadata import (  # noqa: E402
    TraversalPolicy,
    find_node,
    load_catalog,
    load_manifest,
)
from rowtrace.project import DbtProject, RowtraceError  # noqa: E402
from rowtrace.traverse import (  # noqa: E402
    OUTCOME_COMPLETE,
    OUTCOME_START_PRESENT,
    trace_upstream,
)

# Fasit for traversering from mart_summary. Utledet by byggeplanens
# scenario table, ikke by a previous run — ellers we only test at
# koden gjentar seg itself.
EXPECTED = {
    # 222: kunde uten ordre. Begge foreldre is absent, siden 222 mangler in
    # stg_orders completely. The diamond splits itself therefore first at level 2, and
    # stg_orders is reached via both branches -> real cache hit.
    "222": {
        "outcome": OUTCOME_COMPLETE,
        "present": {"stg_customers"},
        "absent": {
            "mart_summary",
            "prep_customer_orders",
            "prep_active_orders",
            "stg_orders",
            "raw_orders",
        },
        "unvisited": {"prep_deduped", "raw_customers"},
        "queries": 6,
        "min_cache_hits": 1,
        "absent_at_source": {"raw_orders"},
    },
    # 111: kontrollverdi, exists overalt. Startnoden has the value, so
    # the traversal should avbrytes umiddelbart with eget utfall.
    "111": {
        "outcome": OUTCOME_START_PRESENT,
        "present": {"mart_summary"},
        "absent": set(),
        "unvisited": set(),
        "queries": 1,
        "min_cache_hits": 0,
        "absent_at_source": set(),
    },
    # 555: forswins in mart_summary itself. Begge foreldre has the value, so
    # both branches prunes at level 1. Nothing is traversed further.
    "555": {
        "outcome": OUTCOME_COMPLETE,
        "present": {"prep_customer_orders", "prep_active_orders"},
        "absent": {"mart_summary"},
        "unvisited": {"stg_customers", "stg_orders", "raw_customers", "raw_orders"},
        "queries": 3,
        "min_cache_hits": 0,
        "absent_at_source": set(),
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
    p.add_argument("--max-depth", type=int, default=None)
    p.add_argument("--no-cache", action="store_true")
    p.add_argument("--no-seeds", action="store_true")
    p.add_argument("--no-sources", action="store_true")
    p.add_argument("--events", action="store_true", help="Show event stream along the way")
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
    policy = TraversalPolicy(
        include_seeds=not args.no_seeds,
        include_sources=not args.no_sources,
    )

    print(f"project_dir : {project.project_dir}")
    print(f"start       : {args.from_node}   {args.key}={args.value}")
    print(f"cache       : {'by' if args.no_cache else 'on'}")
    print(f"max_depth   : {args.max_depth if args.max_depth is not None else 'ubegrenset'}")

    manifest = load_manifest(project)
    catalog = load_catalog(project, required=False)
    if catalog is None:
        print("\nWARNING: catalog.json is missing. Run 'dbt docs generate'.")

    start_uid = find_node(manifest, args.from_node)

    def on_event(ev: dict) -> None:
        if args.events:
            print(f"    [event] {ev}")

    hdr("Traversering")
    result = trace_upstream(
        start_uid,
        args.key,
        args.value,
        project=project,
        manifest=manifest,
        catalog=catalog,
        policy=policy,
        use_cache=not args.no_cache,
        max_depth=args.max_depth,
        on_event=on_event if args.events else None,
    )

    for level in result.by_depth():
        depth = level[0].depth
        print(f"\n  dybde {depth}:")
        for node in level:
            marks = []
            if node.from_cache:
                marks.append("cache")
            if node.pruned:
                marks.append(f"stopp={node.prune_reason}")
            if node.absent_at_source:
                marks.append("ABSENT_AT_SOURCE")
            secs = f"{node.elapsed_s:.2f}s" if node.elapsed_s else "-"
            tag = f"  [{', '.join(marks)}]" if marks else ""
            print(f"    {node.name:<24} {node.status:<28} {secs:>7}{tag}")

    hdr("Kanter (child -> foreldre)")
    for child, parents in result.edges.items():
        child_name = result.checked[child].name
        names = [
            result.checked[p].name if p in result.checked else p.split(".")[-1]
            for p in parents
        ]
        print(f"  {child_name} -> {names}")

    hdr("Statistikk")
    s = result.stats
    print(f"  utfall            : {result.outcome}")
    print(f"  nodes besøkt      : {s.nodes_visited}")
    print(f"  spørringer kjørt  : {s.queries_executed}")
    print(f"  cache-treff       : {s.cache_hits}")
    print(f"  grener stoppet    : {s.branches_pruned}")
    print(f"  største dybde     : {s.max_depth_reached}")
    print(f"  tid in spørringer  : {s.total_query_seconds:.2f}s")

    if result.warnings:
        hdr("Advarsler")
        for w in result.warnings:
            print(f"  {w}")

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

        got_present = {n.name for n in result.checked.values() if n.status == "present"}
        got_absent = {n.name for n in result.checked.values() if n.status == "absent"}
        visited = {n.name for n in result.checked.values()}

        if got_present != expected["present"]:
            failures.append(
                f"present: forexpected {sorted(expected['present'])}, "
                f"got {sorted(got_present)}"
            )
        if got_absent != expected["absent"]:
            failures.append(
                f"absent: forexpected {sorted(expected['absent'])}, "
                f"got {sorted(got_absent)}"
            )

        leaked = visited & expected["unvisited"]
        if leaked:
            failures.append(
                f"besøkte nodes that skulle vært prunet bort: {sorted(leaked)}"
            )

        got_aas = {n.name for n in result.absent_at_source()}
        if got_aas != expected["absent_at_source"]:
            failures.append(
                f"absent_at_source: forexpected {sorted(expected['absent_at_source'])}, "
                f"got {sorted(got_aas)}"
            )

        # Cache-forventningen gjelder kun with cache on. with --no-cache is
        # poenget nettopp at spørringstallet should stige.
        if not args.no_cache:
            if s.queries_executed != expected["queries"]:
                failures.append(
                    f"spørringer: forexpected {expected['queries']}, "
                    f"got {s.queries_executed}"
                )
            if s.cache_hits < expected["min_cache_hits"]:
                failures.append(
                    f"cache-treff: forexpected minst {expected['min_cache_hits']}, "
                    f"got {s.cache_hits}"
                )
        else:
            print(f"  (--no-cache: spørringstall {s.queries_executed} ikke sjekket)")

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
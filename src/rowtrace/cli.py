"""Steg 8 — CLI (architectur.md §13), rendyrket i steg 8.6, forenklet i 8.7.

Bundet sammen: metadata (steg 2) -> traversering (steg 3) -> fold (steg 4)
-> output (steg 7). Diagnoselaget (steg 5/6/6.5: join-/filteranalyse) er
koblet fra hovedstien i steg 8.6 — verktøyet gjør ingen vurdering av funn
lenger, kun ren presence-rapportering. Kildekoden i joins.py/filters.py/
sqlparsing.py er bevart, ikke slettet, men ingenting herfra kaller den
lenger.

Fjernet i steg 8.6, ikke bare skjult:
  --fail-on          - verktøyet vurderer ikke lenger funn. Exit-kode
                       reduseres til 0 (kjørte ferdig) / 2 (verktøyfeil).
  --dialect          - mistet funksjon når SQL-parsing ble koblet fra.
  --quiet / -q        - stats-linjen den kuttet er borte fra default, og
                       warnings vises nå alltid uansett.
  --include-checked  - overflødig: ny default viser alt sjekket per nivå.

Fortsatt bevisst UTENFOR omfang (uendret fra steg 8):
Steg 8.7 — CLI-forenkling til plain v1:
  `trace`-subkommandoen er fjernet. Kommandoformen er nå
  `dgt key,value,from [flagg]` — ett positional-argument, komma-splittet
  til nøyaktig 3 deler. Feil antall deler -> RowtraceError -> exit 2,
  samme feilvei som resten av verktøyet (fanges i main()).

  Fjernet fra --help (kode/default urørt, help=argparse.SUPPRESS):
  --target, --manifest, --catalog, --max-depth, --no-cache, --no-seeds,
  --no-sources, --no-snapshots. --project-dir/--profiles-dir er fortsatt
  synlige, men default=None i argparse (ikke "."), fordi presedensen mot
  .dgt.yml (config.py) må kunne skille "ikke oppgitt av bruker" fra
  "eksplisitt cwd". Selve default-oppløsningen ("." for project_dir) skjer
  i _resolve_paths() her i cli.py, ikke i project.py — DbtProject.create()
  er uendret og vet fortsatt ingenting om .dgt.yml (Beslutning E holdes
  intakt: prosjektsti er alltid en parameter inn til project.py).

  .dgt.yml (valgfri, cwd, se config.py) gir defaults for --project-dir/
  --profiles-dir. Presedens: CLI-flagg > .dgt.yml > hardkodet "." (kun
  project_dir — profiles_dir har ingen egen fallback her, forblir None
  til dbt sin egen standardoppdagelse i DbtProject.create()).
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
from datetime import datetime, timezone
from typing import Any
from .config import load_dgt_config

from . import __version__
from .boundaries import fold_boundaries
from .metadata import TraversalPolicy, find_node, load_catalog, load_manifest
from .output import build_report
from .project import DbtProject, RowtraceError
from .traverse import (
    PRUNE_EXCLUDED,
    PRUNE_MAX_DEPTH,
    PRUNE_MISSING_KEY_COLUMN,
    PRUNE_PRESENT,
    PRUNE_QUERY_ERROR,
    PRUNE_TERMINAL,
    trace_upstream,
)

__all__ = ["build_parser", "main"]

_PRUNE_LABELS = {
    PRUNE_PRESENT: "stoppet her",
    PRUNE_TERMINAL: "kilde",
    PRUNE_MISSING_KEY_COLUMN: "kolonne mangler, hoppet over",
    PRUNE_QUERY_ERROR: "spørring feilet",
    PRUNE_EXCLUDED: "utenfor policy",
    PRUNE_MAX_DEPTH: "maks dybde nådd",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dgt",
        description="dagtrace — ren presence-lineage for dbt",
    )
    parser.add_argument(
        "spec",
        metavar="key,value,from",
        help="Nøkkelkolonne, verdi og startmodell, komma-separert. "
             "Eksempel: orgnr,222,mart_summary",
    )
    parser.add_argument("--project-dir", default=None, help="dbt-prosjektets rotmappe")
    parser.add_argument("--profiles-dir", default=None, help="Mappe med profiles.yml")
    parser.add_argument("--output", "-o", choices=("text", "json", "ndjson"), default="text")
    parser.add_argument(
        "--full-trace", action="store_true",
        help="Ikke stopp på present — fortsett å spore hver gren til dens "
             "terminalkilde selv når verdien finnes underveis. Flere "
             "spørringer enn standard (ingen pruning på present-grener).",
    )

    # Fjernet fra --help i steg 8.7, kode/default urørt (se docstring øverst).
    parser.add_argument("--target", "-t", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--manifest", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--catalog", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--max-depth", type=int, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--no-cache", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--no-seeds", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--no-sources", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--no-snapshots", action="store_true", help=argparse.SUPPRESS)

    return parser


def _presence_symbol(status: str) -> str:
    if status == "present":
        return "✅"
    if status == "absent":
        return "❌"
    return "⚠️"

def _parse_spec(spec: str) -> tuple[str, str, str]:
    """key,value,from -> (key, value, from_node). Krever eksakt 3 deler."""
    parts = spec.split(",")
    if len(parts) != 3:
        raise RowtraceError(
            f"Forventet nøyaktig 3 komma-separerte deler (key,value,from), "
            f"fikk {len(parts)} i {spec!r}."
        )
    key, value, from_node = parts
    if not key or not value or not from_node:
        raise RowtraceError(f"Tom del i key,value,from: {spec!r}")
    return key, value, from_node

def _resolve_paths(args: argparse.Namespace) -> tuple[str, str | None]:
    """Presedens (steg 8.7): CLI-flagg -> .dgt.yml -> hardkodet default.

    project_dir får alltid en verdi ("." som siste fallback). profiles_dir
    kan forbli None — DbtProject.create() lar da dbt selv falle tilbake
    til sin egen standardoppdagelse (~/.dbt/ eller DBT_PROFILES_DIR).
    """
    config = load_dgt_config()

    project_dir = args.project_dir
    if project_dir is None:
        project_dir = config.get("project_dir", ".")

    profiles_dir = args.profiles_dir
    if profiles_dir is None:
        profiles_dir = config.get("profiles_dir")

    return project_dir, profiles_dir


# --------------------------------------------------------------------------
# Spinner — animert "Working..." mens hovedtråden venter på dbt show.
# --------------------------------------------------------------------------


class _Spinner:
    """Egen tråd som animerer 'Working' med prikker som beveger seg, mens
    hovedtråden er blokkert i et dbt show-kall. Kun én skriver til stdout
    av gangen — stop() blokkerer til tråden faktisk har stanset, før
    _ProgressivePrinter skriver resultatlinjer over samme sted.
    """

    _FRAMES = ["   ", ".  ", ".. ", "..."]
    _INTERVAL = 0.3

    def __init__(self, label: str = "Working") -> None:
        self._label = label
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        i = 0
        while not self._stop.is_set():
            frame = self._FRAMES[i % len(self._FRAMES)]
            sys.stdout.write(f"\r\033[2K{self._label}{frame}")
            sys.stdout.flush()
            i += 1
            self._stop.wait(self._INTERVAL)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
        sys.stdout.write("\r\033[2K")
        sys.stdout.flush()


# --------------------------------------------------------------------------
# Tekstrendring — nivåbasert, progressiv (steg 8.6 + menneskelig test).
#
# Bygger utelukkende på on_event-hendelser ('node_checked'/'branch_pruned')
# fra traverse.py, ikke på et ferdig TraceResult. En node nådd via flere
# foreldre (diamant) samles i samme nivå-buffer og vises kun én gang, med
# en "nådd fra"-liste — result.checked er allerede deduplisert per
# node-UID, og her følges samme prinsipp direkte på hendelsesstrømmen.
# --------------------------------------------------------------------------


class _ProgressivePrinter:
    def __init__(self) -> None:
        self._current_depth: int | None = None
        self._buffer: dict[str, tuple[str, str, str | None]] = {}
        self._terminal: dict[str, bool] = {}
        self._names: dict[str, str] = {}
        self._reached: dict[str, list[str]] = {}
        self._spinner = _Spinner()
        self._spinner_running = False

    def _start_spinner(self) -> None:
        if not self._spinner_running:
            self._spinner.start()
            self._spinner_running = True

    def _stop_spinner(self) -> None:
        if self._spinner_running:
            self._spinner.stop()
            self._spinner_running = False

    def handle(self, ev: dict[str, Any]) -> None:
        if ev.get("event") not in ("node_checked", "branch_pruned"):
            return
        depth = ev.get("depth")
        node_uid = ev.get("node")
        if depth is None or node_uid is None:
            return

        name = ev.get("name") or node_uid.split(".")[-1]
        self._names[node_uid] = name
        self._terminal[node_uid] = bool(ev.get("terminal", False))

        reached_from_uid = ev.get("reached_from")
        if reached_from_uid:
            child_name = self._names.get(reached_from_uid, reached_from_uid)
            lst = self._reached.setdefault(node_uid, [])
            if child_name not in lst:
                lst.append(child_name)

        if self._current_depth is None:
            self._current_depth = depth
        elif depth != self._current_depth:
            self._stop_spinner()
            self._flush()
            self._start_spinner()
            self._current_depth = depth

        presence = ev.get("presence", "unknown")
        prune_reason = ev.get("reason")
        elapsed_s = ev.get("elapsed_s")
        from_cache = bool(ev.get("from_cache", False))
        self._buffer[node_uid] = (name, presence, prune_reason, elapsed_s, from_cache)

        if depth == 0:
            # Startnoden er alltid alene på nivå 0 — flush umiddelbart
            # i stedet for å vente på et nivåskifte som aldri kommer før
            # dette punktet.
            self._stop_spinner()
            self._flush()
            self._start_spinner()
            self._current_depth = None

    def _flush(self) -> None:
        if not self._buffer:
            return
        depth = self._current_depth
        prefix = f"Level {depth}:"
        pad = " " * len(prefix)
        items = sorted(self._buffer.items(), key=lambda kv: kv[1][0])
        for i, (node_uid, (name, presence, prune_reason, elapsed_s, from_cache)) in enumerate(items):
            label = prefix if i == 0 else pad
            symbol = _presence_symbol(presence)

            tag = ""
            if depth == 0:
                tag = "(start)"
            elif self._terminal.get(node_uid) and presence == "absent":
                tag = "[ABSENT_AT_SOURCE]"
            elif self._terminal.get(node_uid) and presence == "present":
                tag = "[KILDE]"
            elif prune_reason:
                tag = f"— {_PRUNE_LABELS.get(prune_reason, prune_reason)}"

            if from_cache:
                time_txt = "  (cache)"
            elif elapsed_s is not None:
                time_txt = f"  ({elapsed_s:.2f}s)"
            else:
                time_txt = ""

            reached = self._reached.get(node_uid)
            reached_txt = f"  (nådd fra: {', '.join(reached)})" if reached else ""

            print(f"{label} {name:<28} {symbol} {presence:<10}{tag}{time_txt}{reached_txt}")
        print("-" * 60)
        self._buffer = {}

    def finish(self) -> None:
        self._stop_spinner()
        self._flush()


# --------------------------------------------------------------------------
# ndjson - hendelsesstrøm (uendret struktur, architectur.md §12.5)
# --------------------------------------------------------------------------


def _emit_ndjson(event: dict[str, Any]) -> None:
    print(json.dumps(event, ensure_ascii=False))


def _run_trace(args: argparse.Namespace) -> int:
    key, value, from_node = _parse_spec(args.spec)
    project_dir, profiles_dir = _resolve_paths(args)

    project = DbtProject.create(
        project_dir=project_dir,
        profiles_dir=profiles_dir,
        target=args.target,
        manifest_path=args.manifest,
        catalog_path=args.catalog,
    )
    policy = TraversalPolicy(
        include_seeds=not args.no_seeds,
        include_sources=not args.no_sources,
        include_snapshots=not args.no_snapshots,
    )

    manifest = load_manifest(project)
    catalog = load_catalog(project, required=False)
    start_uid = find_node(manifest, from_node)

    text = args.output == "text"
    ndjson = args.output == "ndjson"

    printer = _ProgressivePrinter() if text else None

    if text:
        print(f"start   : {start_uid}   {key}={value}")
        print()
    if ndjson:
        _emit_ndjson(
            {"event": "run_started", "start_node": start_uid, "key": key, "value": value}
        )

    def on_event(ev: dict[str, Any]) -> None:
        if text and printer is not None:
            printer.handle(ev)
        if ndjson and ev.get("event") in ("node_checked", "branch_pruned"):
            _emit_ndjson(ev)

    started_dt = datetime.now(timezone.utc)
    result = trace_upstream(
        start_uid, key, value,
        project=project, manifest=manifest, catalog=catalog,
        policy=policy, use_cache=not args.no_cache, max_depth=args.max_depth,
        full_trace=args.full_trace,
        on_event=on_event,
    )
    finished_dt = datetime.now(timezone.utc)
    started_at = started_dt.isoformat()
    finished_at = finished_dt.isoformat()
    wall_seconds = (finished_dt - started_dt).total_seconds()

    if text:
        printer.finish()
        print()
        print(f"status  : {result.outcome}")
        print()
        print("Statistikk:")
        print(f"  noder besøkt        : {result.stats.nodes_visited}")
        print(f"  spørringer kjørt    : {result.stats.queries_executed}")
        print(f"  cache-treff         : {result.stats.cache_hits}")
        print(f"  grener prunet       : {result.stats.branches_pruned}")
        print(f"  tid i spørringer    : {result.stats.total_query_seconds:.2f}s")
        print(f"  total kjøretid      : {wall_seconds:.2f}s")
        if result.warnings:
            print()
            print("Advarsler:")
            for w in result.warnings:
                    print(f"  {w}")
        return 2 if result.outcome in ("start_not_checkable", "start_error") else 0

    # json / ndjson trenger fortsatt boundary-folding for boundaries/absent_at_source
    report = fold_boundaries(result)
    doc = build_report(
        result, report,
        tool_version=__version__, started_at=started_at, finished_at=finished_at,
        target=args.target,
    )

    if ndjson:
        for b in doc["boundaries"]:
            _emit_ndjson({"event": "finding", "kind": "boundary", "finding": b})
        for a in doc["absent_at_source"]:
            _emit_ndjson({"event": "finding", "kind": "absent_at_source", "finding": a})
        for w in doc["warnings"]:
            _emit_ndjson({"event": "warning", "message": w})
        _emit_ndjson(
            {"event": "run_finished", "status": doc["status"], "stats": doc["run"]["stats"]}
        )
    else:
        print(json.dumps(doc, indent=2, ensure_ascii=False))

    return 2 if doc["status"] == "failed" else 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        return _run_trace(args)
    except RowtraceError as exc:
        print(f"Verktøyfeil: {exc}", file=sys.stderr)
        return 2
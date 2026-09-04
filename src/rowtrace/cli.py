"""Step 8 — CLI (architectur.md §13), refined in step 8.6, simplified in 8.7.

Tied together: metadata (step 2) -> traversal (step 3) -> fold (step 4)
-> output (step 7). The diagnostic layer (step 5/6/6.5: join/filter analysis)
is disconnected from the main path in step 8.6 — the tool no longer evaluates
findings, only pure presence reporting. The source code in joins.py/filters.py/
sqlparsing.py is preserved, not deleted, but nothing from here calls it
anymore.

Removed in step 8.6, not just hidden:
  --fail-on          - the tool no longer evaluates findings. Exit code
                       reduced to 0 (completed successfully) / 2 (tool error).
  --dialect          - lost function when SQL parsing was disconnected.
  --quiet / -q       - the stats line it cut is gone from default, and
                       warnings are now always shown regardless.
  --include-checked  - redundant: new default shows everything checked per level.

Still deliberately OUT OF scope (unchanged from step 8):
Step 8.7 — CLI simplification to plain v1:
  The `trace` subcommand is removed. Command form is now
  `dgt key,value,from [flags]` — one positional argument, comma-split
  into exactly 3 parts. Wrong number of parts -> RowtraceError -> exit 2,
  same error path as rest of tool (caught in main()).

  Removed from --help (code/default untouched, help=argparse.SUPPRESS):
  --target, --manifest, --catalog, --max-depth, --no-cache, --no-seeds,
  --no-sources, --no-snapshots. --project-dir/--profiles-dir are still
  visible, but default=None in argparse (not "."), because precedence against
  .dgt.yml (config.py) must be able to distinguish "not provided by user" from
  "explicit cwd". The actual default resolution ("." for project_dir) happens
  in _resolve_paths() here in cli.py, not in project.py — DbtProject.create()
  is unchanged and still knows nothing about .dgt.yml (Decision E remains
  intact: project path is always a parameter into project.py).

  .dgt.yml (optional, cwd, see config.py) provides defaults for --project-dir/
  --profiles-dir. Precedence: CLI flags > .dgt.yml > hardcoded "." (only
  project_dir — profiles_dir has no fallback here, remains None
  for dbt's own standard discovery in DbtProject.create()).
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
    PRUNE_PRESENT: "stopped here",
    PRUNE_TERMINAL: "source",
    PRUNE_MISSING_KEY_COLUMN: "column missing, skipped",
    PRUNE_QUERY_ERROR: "query failed",
    PRUNE_EXCLUDED: "outside policy",
    PRUNE_MAX_DEPTH: "max depth reached",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dgt",
        description="dagtrace — ren presence-lineage for dbt",
    )
    parser.add_argument(
        "spec",
        metavar="key,value,from",
        help="Key column, value, and start model, comma-separated. "
             "Example: orgnr,222,mart_summary",
    )
    parser.add_argument("--project-dir", default=None, help="dbt project root directory")
    parser.add_argument("--profiles-dir", default=None, help="Directory containing profiles.yml")
    parser.add_argument("--output", "-o", choices=("text", "json", "ndjson"), default="text")
    parser.add_argument(
        "--full-trace", action="store_true",
        help="Don't stop on present — continue tracing each branch to its "
             "terminal source even when the value is found along the way. More "
             "queries than standard (no pruning on present branches).",
    )

    # Removed from --help in step 8.7, code/default untouched (see docstring above).
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
    """key,value,from -> (key, value, from_node). Requires exactly 3 parts."""
    parts = spec.split(",")
    if len(parts) != 3:
        raise RowtraceError(
            f"Expected exactly 3 comma-separated parts (key,value,from), "
            f"got {len(parts)} in {spec!r}."
        )
    key, value, from_node = parts
    if not key or not value or not from_node:
        raise RowtraceError(f"Empty part in key,value,from: {spec!r}")
    return key, value, from_node

def _resolve_paths(args: argparse.Namespace) -> tuple[str, str | None]:
    """Precedence (step 8.7): CLI flags -> .dgt.yml -> hardcoded default.

    project_dir always gets a value ("." as last fallback). profiles_dir
    can remain None — DbtProject.create() then lets dbt itself fall back
    to its own standard discovery (~/.dbt/ or DBT_PROFILES_DIR).
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
# Spinner — animated "Working..." while main thread waits on dbt show.
# --------------------------------------------------------------------------


class _Spinner:
    """Separate thread that animates 'Working' with moving dots, while
    the main thread is blocked in a dbt show call. Only one writer to stdout
    at a time — stop() blocks until the thread has actually stopped, before
    _ProgressivePrinter writes result lines over the same spot.
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
# Text rendering — level-based, progressive (step 8.6 + human testing).
#
# Built exclusively on on_event events ('node_checked'/'branch_pruned')
# from traverse.py, not on a finished TraceResult. A node reached via multiple
# parents (diamond) is collected in the same level buffer and shown only once,
# with a "reached from" list — result.checked is already deduplicated per
# node UID, and the same principle is followed directly on the event stream here.
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
            # Start node is always alone at level 0 — flush immediately
            # instead of waiting for a level change that never comes before
            # this point.
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
            reached_txt = f"  (reached from: {', '.join(reached)})" if reached else ""

            print(f"{label} {name:<28} {symbol} {presence:<10}{tag}{time_txt}{reached_txt}")
        print("-" * 60)
        self._buffer = {}

    def finish(self) -> None:
        self._stop_spinner()
        self._flush()


# --------------------------------------------------------------------------
# ndjson - event stream (unchanged structure, architectur.md §12.5)
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
        print("Statistics:")
        print(f"  nodes visited       : {result.stats.nodes_visited}")
        print(f"  queries executed    : {result.stats.queries_executed}")
        print(f"  cache hits          : {result.stats.cache_hits}")
        print(f"  branches pruned     : {result.stats.branches_pruned}")
        print(f"  time in queries     : {result.stats.total_query_seconds:.2f}s")
        print(f"  total runtime       : {wall_seconds:.2f}s")
        if result.warnings:
            print()
            print("Warnings:")
            for w in result.warnings:
                    print(f"  {w}")
        return 2 if result.outcome in ("start_not_checkable", "start_error") else 0

    # json / ndjson still need boundary-folding for boundaries/absent_at_source
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
        print(f"Tool error: {exc}", file=sys.stderr)
        return 2
"""Command-line entry point for `dgt`."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dgt import __version__
from dgt.config import resolve_config
from dgt.errors import DgtError
from dgt.graph import DagGraph, Direction
from dgt.output import (
    RunMeta,
    build_document,
    error_document,
    render_json,
    render_ndjson,
    render_ndjson_error,
    render_text,
    utc_timestamp,
)
from dgt.probes import (
    DBT_SHOW_TIMEOUT,
    DbtShow,
    ValueProbe,
    find_dbt_executable,
    quote_ident,
    sql_literal,
)
from dgt.project import DbtProject
from dgt.traverse import trace_column

EXIT_OK = 0
EXIT_TOOL_ERROR = 2

DIRECTIONS = {
    "up": [Direction.UP],
    "down": [Direction.DOWN],
    "both": [Direction.UP, Direction.DOWN],
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dgt",
        description="Trace where a column, or values in it, appear or disappear in a dbt DAG.",
    )
    parser.add_argument("start_model", help="model/seed/snapshot/source name or unique_id")
    parser.add_argument("-c", "--column", required=True, help="column to trace")
    values = parser.add_mutually_exclusive_group()
    values.add_argument(
        "-v", "--value", dest="values", action="append", metavar="VALUE",
        help="value in the column to trace (repeatable)",
    )
    values.add_argument(
        "--values-file", type=Path, metavar="FILE", help="file with one value per line"
    )
    parser.add_argument(
        "-d", "--direction", choices=("up", "down", "both"), default="up",
        help="traversal direction (default: up)",
    )
    parser.add_argument(
        "--full-trace", action="store_true",
        help="do not stop at status changes; map everything reachable",
    )
    parser.add_argument(
        "-o", "--output", choices=("text", "json", "ndjson"), default="text",
        help="output format (default: text)",
    )
    parser.add_argument("--project-dir", help="dbt project directory (default: .)")
    parser.add_argument("--profiles-dir", help="dbt profiles directory")
    parser.add_argument("--target", help="dbt target")
    parser.add_argument("--dbt-executable", help=argparse.SUPPRESS)
    parser.add_argument(
        "--dbt-timeout", type=int, default=DBT_SHOW_TIMEOUT, metavar="SECONDS",
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--version", action="version", version=f"dgt {__version__}")
    return parser


def read_values(args: argparse.Namespace) -> list[str]:
    """Values from `-v` or `--values-file`, deduplicated in order.

    Blank lines in the file are skipped.
    """
    if args.values_file is not None:
        try:
            lines = args.values_file.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise DgtError(f"cannot read values file: {exc}") from exc
        values = [line.strip() for line in lines if line.strip()]
    else:
        values = list(args.values or [])
    return list(dict.fromkeys(values))


def main(argv: list[str] | None = None) -> int:
    started_at = utc_timestamp()
    args = build_parser().parse_args(argv)
    target = args.target
    try:
        values = read_values(args)
        if values:
            # Validate up front so bad input fails before any dbt call.
            quote_ident(args.column)
            for value in values:
                sql_literal(value)
        config = resolve_config(args.project_dir, args.profiles_dir, args.target)
        target = config.target
        for warning in config.warnings:
            print(f"dgt: warning: {warning}", file=sys.stderr)
        project = DbtProject.load(config.project_dir)
        start = project.find_node(args.start_model)
        value_probe = None
        if values:
            run_sql = DbtShow(
                find_dbt_executable(args.dbt_executable),
                config.project_dir,
                config.profiles_dir,
                config.target,
                timeout=args.dbt_timeout,
            )
            value_probe = ValueProbe(values, run_sql)
        report = trace_column(
            project, DagGraph(project.manifest), start, args.column,
            DIRECTIONS[args.direction], full_trace=args.full_trace, value_probe=value_probe,
        )
    except DgtError as exc:
        print(f"dgt: error: {exc}", file=sys.stderr)
        meta = RunMeta(__version__, args.direction, target, started_at, utc_timestamp())
        if args.output == "json":
            sys.stdout.write(render_json(error_document(str(exc), meta)))
        elif args.output == "ndjson":
            sys.stdout.write(render_ndjson_error(str(exc), meta))
        return EXIT_TOOL_ERROR

    meta = RunMeta(__version__, args.direction, target, started_at, utc_timestamp())
    if args.output == "json":
        sys.stdout.write(render_json(build_document(report, meta)))
    elif args.output == "ndjson":
        sys.stdout.write(render_ndjson(report, meta))
    else:
        sys.stdout.write(render_text(report))
    return EXIT_OK

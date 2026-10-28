"""Command-line entry point for `dgt`."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dgt import __version__
from dgt.config import resolve_config
from dgt.errors import DgtError
from dgt.graph import DagGraph, Direction
from dgt.output import render_text
from dgt.project import DbtProject
from dgt.traverse import trace_column

EXIT_OK = 0
EXIT_TOOL_ERROR = 2


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
    parser.add_argument("--version", action="version", version=f"dgt {__version__}")
    return parser


def read_values(args: argparse.Namespace) -> list[str]:
    """Values from `-v` or `--values-file`; blank lines in the file are skipped."""
    if args.values_file is not None:
        try:
            lines = args.values_file.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise DgtError(f"cannot read values file: {exc}") from exc
        return [line.strip() for line in lines if line.strip()]
    return list(args.values or [])


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        _check_supported(args)
        config = resolve_config(args.project_dir, args.profiles_dir, args.target)
        project = DbtProject.load(config.project_dir)
        start = project.find_node(args.start_model)
        report = trace_column(
            project, DagGraph(project.manifest), start, args.column,
            [Direction(args.direction)], full_trace=args.full_trace,
        )
    except DgtError as exc:
        print(f"dgt: error: {exc}", file=sys.stderr)
        return EXIT_TOOL_ERROR
    sys.stdout.write(render_text(report))
    return EXIT_OK


def _check_supported(args: argparse.Namespace) -> None:
    if read_values(args):
        raise DgtError("value tracing (-v/--values-file) is not implemented yet")
    if args.direction != "up":
        raise DgtError(f"direction '{args.direction}' is not implemented yet")
    if args.output != "text":
        raise DgtError(f"output format '{args.output}' is not implemented yet")

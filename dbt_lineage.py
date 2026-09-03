#!/usr/bin/env python3
"""Kjørbar inngang til dbt-lineage CLI-et i dette (uinstallerte) repoet.

Kjør:
    python dbt_lineage.py trace --key orgnr --value 222 --from mart_summary \
        --project-dir testproject --profiles-dir testproject

Når verktøyet pakkes for PyPI (architectur.md §10/§11, senere fase) blir
dette en konsoll-script entry point ('dbt-lineage = rowtrace.cli:main' i
pyproject.toml) og denne fila blir overflødig. Steg 8 sitt omfang er selve
CLI-logikken (cli.py), ikke pakking - denne fila er kun midlertidig lim.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from rowtrace.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
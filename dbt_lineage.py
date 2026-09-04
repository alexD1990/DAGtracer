#!/usr/bin/env python3
"""Runnable entry point to the dbt-lineage CLI in this (uninstalled) repo.

Run:
    python dbt_lineage.py trace --key orgnr --value 222 --from mart_summary \
        --project-dir testproject --profiles-dir testproject

When the tool is packaged for PyPI (architectur.md §10/§11, later phase), this
becomes a console-script entry point ('dbt-lineage = rowtrace.cli:main' in
pyproject.toml) and this file becomes redundant. Step 8's scope is the
CLI logic itself (cli.py), not packaging - this file is only temporary glue.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from rowtrace.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
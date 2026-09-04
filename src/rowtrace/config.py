"""Step 8.7 — .dgt.yml: optional project-local config for project_dir/profiles_dir.

Read from cwd if the file exists. No auto-creation in v1 — the user
creates it manually (documented in README). Precedence is NOT this
module's responsibility: it only reads and validates. The precedence itself
(CLI flags -> .dgt.yml -> hardcoded default) is enforced together in one place,
in cli.py's _resolve_paths(), cf. Decision E in byggeplan.md.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from .project import RowtraceError

CONFIG_FILENAME = ".dgt.yml"

_ALLOWED_KEYS = {"project_dir", "profiles_dir"}


def load_dgt_config(cwd: Path | None = None) -> dict[str, str]:
    """Read .dgt.yml from cwd. Returns {} if file doesn't exist.

    Raises RowtraceError (caught in cli.py -> exit 2) on:
    - invalid YAML syntax
    - file contains something other than a YAML object (e.g. a list)
    - project_dir/profiles_dir has a value that isn't a text string

    Unknown keys in file are silently ignored (future-proofing, not an error).
    """
    base = cwd or Path.cwd()
    path = base / CONFIG_FILENAME
    if not path.is_file():
        return {}

    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RowtraceError(f"Could not read {path}: {exc}") from exc

    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise RowtraceError(f"Invalid YAML in {path}: {exc}") from exc

    if data is None:
        return {}  # empty file
    if not isinstance(data, dict):
        raise RowtraceError(
            f"{path} must contain a YAML object "
            f"(project_dir: ..., profiles_dir: ...), found {type(data).__name__}."
        )

    result: dict[str, str] = {}
    for key, value in data.items():
        if key not in _ALLOWED_KEYS:
            continue
        if value is None:
            continue
        if not isinstance(value, str):
            raise RowtraceError(
                f"{path}: '{key}' must be a text string, found {type(value).__name__}."
            )
        result[key] = value

    return result
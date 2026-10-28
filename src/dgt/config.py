"""Resolve run configuration from CLI flags, `.dgt.yml` and defaults.

Precedence per setting: CLI flag > `.dgt.yml` in the current directory > default.
The only default is `project_dir="."`. `profiles_dir` and `target` have no
fallback: when unset they are not passed to dbt, which then applies its own
lookup rules.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from dgt.errors import ConfigError

CONFIG_FILENAME = ".dgt.yml"
CONFIG_KEYS = ("project_dir", "profiles_dir", "target")


@dataclass(frozen=True)
class Config:
    project_dir: Path
    profiles_dir: Path | None
    target: str | None
    warnings: tuple[str, ...] = field(default=())


def load_config_file(cwd: Path) -> tuple[dict[str, str], list[str]]:
    """Read `.dgt.yml` from `cwd`; returns (values, warnings).

    Missing file gives no values. Null values are skipped. Unknown keys are
    not an error, so newer config files still load, but each one produces a
    warning so typos like `profile_dir` are noticed.
    """
    path = cwd / CONFIG_FILENAME
    if not path.is_file():
        return {}, []
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML: {exc}") from exc
    if data is None:
        return {}, []
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: expected a mapping at the top level")
    warnings = [_unknown_key_warning(path, key) for key in data if key not in CONFIG_KEYS]
    result: dict[str, str] = {}
    for key in CONFIG_KEYS:
        value = data.get(key)
        if value is None:
            continue
        if not isinstance(value, str) or not value:
            raise ConfigError(f"{path}: '{key}' must be a non-empty string")
        result[key] = value
    return result, warnings


def _unknown_key_warning(path: Path, key: object) -> str:
    hint = difflib.get_close_matches(str(key), CONFIG_KEYS, n=1)
    suggestion = f"; did you mean '{hint[0]}'?" if hint else f"; known keys: {', '.join(CONFIG_KEYS)}"
    return f"{path}: unknown key '{key}' ignored{suggestion}"


def resolve_config(
    project_dir: str | None = None,
    profiles_dir: str | None = None,
    target: str | None = None,
    cwd: Path | None = None,
) -> Config:
    """Merge CLI values with `.dgt.yml` in `cwd`. Relative paths resolve against `cwd`."""
    cwd = (cwd or Path.cwd()).resolve()
    file_values, warnings = load_config_file(cwd)

    def pick(cli_value: str | None, key: str) -> str | None:
        return cli_value if cli_value is not None else file_values.get(key)

    resolved_profiles = pick(profiles_dir, "profiles_dir")
    return Config(
        project_dir=_absolute(pick(project_dir, "project_dir") or ".", cwd),
        profiles_dir=_absolute(resolved_profiles, cwd) if resolved_profiles else None,
        target=pick(target, "target"),
        warnings=tuple(warnings),
    )


def _absolute(value: str, cwd: Path) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else (cwd / path).resolve()

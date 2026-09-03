"""Steg 8.7 — .dgt.yml: valgfri prosjektlokal config for project_dir/profiles_dir.

Leses fra cwd hvis fila finnes. Ingen auto-opprettelse i v1 — brukeren
oppretter den manuelt (dokumentert i README). Presedens er IKKE denne
modulens ansvar: den leser og validerer bare. Selve presedensen
(CLI-flagg -> .dgt.yml -> hardkodet default) håndheves samlet ett sted,
i cli.py sin _resolve_paths(), jf. Beslutning E i byggeplan.md.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from .project import RowtraceError

CONFIG_FILENAME = ".dgt.yml"

_ALLOWED_KEYS = {"project_dir", "profiles_dir"}


def load_dgt_config(cwd: Path | None = None) -> dict[str, str]:
    """Les .dgt.yml fra cwd. Returnerer {} hvis fila ikke finnes.

    Kaster RowtraceError (fanges i cli.py -> exit 2) ved:
    - ugyldig YAML-syntaks
    - fila inneholder noe annet enn et YAML-objekt (f.eks. en liste)
    - project_dir/profiles_dir har en verdi som ikke er en tekststreng

    Ukjente nøkler i fila ignoreres stille (fremtidssikring, ikke en feil).
    """
    base = cwd or Path.cwd()
    path = base / CONFIG_FILENAME
    if not path.is_file():
        return {}

    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RowtraceError(f"Kunne ikke lese {path}: {exc}") from exc

    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise RowtraceError(f"Ugyldig YAML i {path}: {exc}") from exc

    if data is None:
        return {}  # tom fil
    if not isinstance(data, dict):
        raise RowtraceError(
            f"{path} må inneholde et YAML-objekt "
            f"(project_dir: ..., profiles_dir: ...), fant {type(data).__name__}."
        )

    result: dict[str, str] = {}
    for key, value in data.items():
        if key not in _ALLOWED_KEYS:
            continue
        if value is None:
            continue
        if not isinstance(value, str):
            raise RowtraceError(
                f"{path}: '{key}' må være en tekststreng, fant {type(value).__name__}."
            )
        result[key] = value

    return result
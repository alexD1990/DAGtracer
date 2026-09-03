"""Delt prosjektkontekst og feiltyper.

Beslutning E (docs/byggeplan.md): prosjektsti og profilsti er alltid
parametere. Verktøyets repo og dbt-prosjektet det kjører mot er to
separate mapper.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


class RowtraceError(RuntimeError):
    """Basefeil. Alt som bobler ut hit blir exit-kode 2 (architectur.md §13.3)."""


class MetadataError(RowtraceError):
    """Manifest/catalog mangler, er ugyldig, eller noden finnes ikke."""


class PresenceError(RowtraceError):
    """dbt-kallet eller parsingen av det feilet."""


@dataclass(frozen=True)
class DbtProject:
    """Peker på dbt-prosjektet verktøyet kjører *mot* — aldri verktøyets eget repo."""

    project_dir: Path
    profiles_dir: Path | None = None
    target: str | None = None
    manifest_path: Path | None = None
    catalog_path: Path | None = None

    @classmethod
    def create(
        cls,
        project_dir: str | Path,
        profiles_dir: str | Path | None = None,
        target: str | None = None,
        manifest_path: str | Path | None = None,
        catalog_path: str | Path | None = None,
    ) -> "DbtProject":
        pd = Path(project_dir).expanduser().resolve()
        if not pd.is_dir():
            raise RowtraceError(f"project_dir finnes ikke: {pd}")

        def _opt(p: str | Path | None) -> Path | None:
            return Path(p).expanduser().resolve() if p else None

        return cls(
            project_dir=pd,
            profiles_dir=_opt(profiles_dir),
            target=target,
            manifest_path=_opt(manifest_path),
            catalog_path=_opt(catalog_path),
        )

    @property
    def manifest_file(self) -> Path:
        return self.manifest_path or (self.project_dir / "target" / "manifest.json")

    @property
    def catalog_file(self) -> Path:
        return self.catalog_path or (self.project_dir / "target" / "catalog.json")
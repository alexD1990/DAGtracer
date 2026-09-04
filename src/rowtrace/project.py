"""Shared project context and error types.

Decision E (docs/byggeplan.md): project path and profile path are always
parameters. The tool's repo and the dbt project it runs against are two
separate directories.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


class RowtraceError(RuntimeError):
    """Base error. Everything that bubbles out here becomes exit code 2 (architectur.md §13.3)."""


class MetadataError(RowtraceError):
    """Manifest/catalog is missing, invalid, or node doesn't exist."""


class PresenceError(RowtraceError):
    """The dbt call or parsing of it failed."""


@dataclass(frozen=True)
class DbtProject:
    """Points to the dbt project the tool runs *against* — never the tool's own repo."""

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
            raise RowtraceError(f"project_dir does not exist: {pd}")

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
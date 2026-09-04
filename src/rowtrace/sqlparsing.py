"""Delt sqlglot-parsing for step 5 (joins.py) and step 6 (filters.py).

Trukket ut i step 7-planleggingen: joins.py and filters.py parset tidligere
same kompilerte SQL-streng each for seg, uavhengig by hverandre, for hver
grensenode that trengte begge deler (diagnose_all). sqlglot.parse_one() is
not free — this fjerner den double cost without changing resultatet
(same streng, same dialekt, alltid same parse-tre).

Belongs here, ikke i metadata.py: metadata.py is manifest-/catalog-
laget (architectur.md §3 lag 1) and should ikke vite om sqlglot. Dette laget
is still diagnostikk-/rapportlag (lag 4), bare den delen that is felles
mellom join and filter analysis.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import sqlglot
from sqlglot import exp

from .metadata import compiled_sql_for_node
from .project import DbtProject

__all__ = ["ParsedNode", "parsed_sql_for_node"]


@dataclass(frozen=True)
class ParsedNode:
    """Resultat by of fetching and parse kompilert SQL for one node."""

    sql: str | None
    tree: exp.Expression | None
    ok: bool
    notes: tuple[str, ...]


def parsed_sql_for_node(
    project: DbtProject, node: dict[str, Any], dialect: str
) -> ParsedNode:
    compiled_sql, source_note = compiled_sql_for_node(project, node)
    notes: list[str] = [source_note] if source_note else []

    if not compiled_sql:
        notes.append(
            "Fant no kompilert SQL, verken i target/compiled/ or i "
            "the manifest. Run 'dbt compile' again."
        )
        return ParsedNode(sql=None, tree=None, ok=False, notes=tuple(notes))

    try:
        tree = sqlglot.parse_one(compiled_sql, read=dialect)
    except Exception as exc:  # sqlglot can kaste flere wrongtyper
        notes.append(f"sqlglot klarte can't parse SQL-en ({dialect}): {exc}")
        return ParsedNode(sql=compiled_sql, tree=None, ok=False, notes=tuple(notes))

    return ParsedNode(sql=compiled_sql, tree=tree, ok=True, notes=tuple(notes))
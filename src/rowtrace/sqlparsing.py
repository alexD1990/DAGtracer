"""Delt sqlglot-parsing for steg 5 (joins.py) og steg 6 (filters.py).

Trukket ut i steg 7-planleggingen: joins.py og filters.py parset tidligere
samme kompilerte SQL-streng hver for seg, uavhengig av hverandre, for hver
grensenode som trengte begge deler (diagnose_all). sqlglot.parse_one() er
ikke gratis — dette fjerner den doble kostnaden uten å endre resultatet
(samme streng, samme dialekt, alltid samme parse-tre).

Hører hjemme her, ikke i metadata.py: metadata.py er manifest-/catalog-
laget (architectur.md §3 lag 1) og skal ikke vite om sqlglot. Dette laget
er fortsatt diagnostikk-/rapportlag (lag 4), bare den delen som er felles
mellom join- og filteranalyse.
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
    """Resultat av å hente og parse kompilert SQL for én node."""

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
            "Fant ingen kompilert SQL, verken i target/compiled/ eller i "
            "manifestet. Kjør 'dbt compile' på nytt."
        )
        return ParsedNode(sql=None, tree=None, ok=False, notes=tuple(notes))

    try:
        tree = sqlglot.parse_one(compiled_sql, read=dialect)
    except Exception as exc:  # sqlglot kan kaste flere feiltyper
        notes.append(f"sqlglot klarte ikke parse SQL-en ({dialect}): {exc}")
        return ParsedNode(sql=compiled_sql, tree=None, ok=False, notes=tuple(notes))

    return ParsedNode(sql=compiled_sql, tree=tree, ok=True, notes=tuple(notes))
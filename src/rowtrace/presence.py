"""Eksekveringslag (architectur.md §3 lag 3, §6).

Metadata-ansvaret er flyttet til rowtrace/metadata.py i steg 2. Denne filen
eier nå kun veien fra (relasjon, kolonne, verdi) til en bool.

Beslutninger som styrer filen (docs/byggeplan.md):
  A - check_presence(node_uid, key_column, value) -> bool er grensesnittet.
      Innmaten (subprocess her) skal kunne byttes til dbtRunner uten at
      kallere røres. Revurderes etter steg 3, ikke før.
  B - node-UID er primærnøkkel internt.
  C - verdien er alltid str, og blir SQL-literal i nøyaktig én funksjon.
  E - prosjektsti og profilsti er parametere, aldri konstanter.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import dataclass
from typing import Any

from .metadata import (
    TraversalPolicy,
    classify_node,
    load_catalog,
    load_manifest,
    relation_for,
)
from .project import DbtProject, MetadataError, PresenceError, RowtraceError

# Bakoverkompatible re-eksporter, slik at step1_test.py fortsatt går.
# Nye kallere bør importere disse fra rowtrace.metadata / rowtrace.project.
from .metadata import find_node as find_model  # noqa: F401

__all__ = [
    "DbtProject",
    "PresenceError",
    "PresenceResult",
    "build_presence_sql",
    "check_presence",
    "check_presence_detailed",
    "check_presence_guarded",
    "parse_count",
    "quote_ident",
    "run_dbt_show",
    "sql_literal",
]


@dataclass(frozen=True)
class PresenceResult:
    """Detaljert utfall. check_presence() koker dette ned til en bool."""

    node_uid: str
    relation: str
    present: bool
    count: int
    sql: str
    elapsed_s: float


@dataclass(frozen=True)
class GuardedResult:
    """Resultat der noden kan ha blitt hoppet over før spørring.

    status følger presence-enumet i architectur.md §12.4 så langt det er
    definert i dag: present | absent | skipped_missing_key_column |
    skipped_query_error | unknown.
    """

    node_uid: str
    name: str
    status: str
    count: int | None = None
    relation: str | None = None
    sql: str | None = None
    elapsed_s: float | None = None
    key_column_type: str | None = None
    detail: str | None = None


# --------------------------------------------------------------------------
# Identifikator og literal - det dialektfølsomme punktet
# --------------------------------------------------------------------------

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def quote_ident(name: str, style: str = "adapter") -> str:
    """Quote et kolonnenavn.

    style="adapter" (standard) genererer Jinja som lener seg på dbt sin egen
    adapter (architectur.md §6) - dbt show --inline kompilerer Jinja, så
    dette virker allerede fra steg 1. Ikke hardkod quoting i Python.
    style="plain" gir rå dobbeltfnutt, kun for feilsøking av generert SQL.
    """
    if not _IDENT_RE.match(name):
        raise PresenceError(
            f"Ugyldig kolonnenavn {name!r}. Tillatt: bokstav/understrek etterfulgt "
            "av bokstaver, tall eller understrek."
        )
    if style == "adapter":
        return '{{ adapter.quote("' + name + '") }}'
    if style == "plain":
        return f'"{name}"'
    raise PresenceError(f"Ukjent quote-style: {style!r}")


# Beslutning C: whitelist-versjon. Bevisst restriktiv - den utvides når et
# reelt prosjekt trenger det, ikke i forkant. Alt som ikke matcher avvises
# høylytt framfor å bli escapet på gjettet dialektvis.
_VALUE_RE = re.compile(r"^[A-Za-z0-9_\-.:+/ ]{1,256}$")


def sql_literal(value: str) -> str:
    """Eneste sted en nøkkelverdi blir til SQL. Ikke dupliser denne logikken.

    Verdien sendes alltid som strengliteral. Warehouset caster selv mot
    tallkolonner; det er tryggere enn at Python gjetter kolonnetypen.
    resolved_type fra catalog (steg 2) eksponeres i output, men styrer
    bevisst *ikke* literal-formen — se GuardedResult.key_column_type.
    """
    if not isinstance(value, str):
        raise PresenceError(
            f"Verdien må være str (beslutning C), fikk {type(value).__name__}."
        )
    if not _VALUE_RE.match(value):
        raise PresenceError(
            f"Verdien {value!r} inneholder tegn utenfor whitelisten "
            "(A-Z a-z 0-9 _ - . : + / mellomrom, maks 256 tegn)."
        )
    return f"'{value}'"


def build_presence_sql(
    relation: str, key_column: str, value: str, quote_style: str = "adapter"
) -> str:
    return (
        f"select count(*) as n from {relation} "
        f"where {quote_ident(key_column, quote_style)} = {sql_literal(value)}"
    )


# --------------------------------------------------------------------------
# Eksekveringslag
# --------------------------------------------------------------------------


def _dbt_show_args(project: DbtProject, sql: str) -> list[str]:
    args = [
        "dbt",
        "show",
        "--inline",
        sql,
        "--output",
        "json",
        "--quiet",
        "--project-dir",
        str(project.project_dir),
    ]
    if project.profiles_dir is not None:
        args += ["--profiles-dir", str(project.profiles_dir)]
    if project.target is not None:
        args += ["--target", project.target]
    return args


def run_dbt_show(project: DbtProject, sql: str, timeout: int = 120) -> tuple[str, float]:
    """Kjør SQL via dbt. Returnerer (stdout, sekunder brukt).

    cwd settes til project_dir fordi relative stier i profiles.yml (f.eks.
    DuckDB sin 'path: testproject.duckdb') løses mot arbeidskatalogen, ikke
    mot --project-dir. Uten dette peker adapteren på feil/tom database.
    """
    args = _dbt_show_args(project, sql)
    started = time.perf_counter()
    try:
        proc = subprocess.run(
            args,
            cwd=str(project.project_dir),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise PresenceError("Fant ikke 'dbt' på PATH.") from exc
    except subprocess.TimeoutExpired as exc:
        raise PresenceError(f"dbt show tidsavbrutt etter {timeout}s.") from exc
    elapsed = time.perf_counter() - started

    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()[-1500:]
        raise PresenceError(
            f"dbt show feilet (exit {proc.returncode}).\nSQL: {sql}\n--- output ---\n{tail}"
        )
    return proc.stdout, elapsed


def parse_count(stdout: str) -> int:
    """Plukk ut count-verdien fra dbt sin JSON-output.

    dbt pretty-printer objektet over flere linjer, så vi kan ikke parse
    linjevis. Ikke gjeninnfør linjevis parsing. I stedet skannes stdout etter
    hvert '{' og forsøkes dekodet med raw_decode; første objekt med en
    rad-liste vinner.

    Verdien hentes posisjonelt (første kolonne i første rad), fordi
    kolonnenavnets casing varierer mellom warehouse.
    """
    decoder = json.JSONDecoder()
    idx = stdout.find("{")

    while idx != -1:
        try:
            payload, end = decoder.raw_decode(stdout, idx)
        except json.JSONDecodeError:
            idx = stdout.find("{", idx + 1)
            continue

        if isinstance(payload, dict):
            rows = payload.get("show")
            if rows is None:
                rows = payload.get("data")
            if isinstance(rows, list):
                if not rows:
                    return 0
                first = rows[0]
                raw = next(iter(first.values())) if isinstance(first, dict) else first
                try:
                    return int(raw)
                except (TypeError, ValueError) as exc:
                    raise PresenceError(f"Uventet count-verdi fra dbt: {raw!r}") from exc

        idx = stdout.find("{", end)

    raise PresenceError(
        "Fant ingen JSON-resultatobjekt i dbt sin output.\n--- stdout ---\n"
        + stdout.strip()[-1500:]
    )


# --------------------------------------------------------------------------
# Beslutning A: det stabile grensesnittet
# --------------------------------------------------------------------------


def check_presence_detailed(
    node_uid: str,
    key_column: str,
    value: str,
    *,
    project: DbtProject,
    manifest: dict[str, Any] | None = None,
    quote_style: str = "adapter",
    timeout: int = 120,
) -> PresenceResult:
    manifest = manifest if manifest is not None else load_manifest(project)
    relation = relation_for(manifest, node_uid)
    sql = build_presence_sql(relation, key_column, value, quote_style)
    stdout, elapsed = run_dbt_show(project, sql, timeout=timeout)
    count = parse_count(stdout)
    return PresenceResult(
        node_uid=node_uid,
        relation=relation,
        present=count > 0,
        count=count,
        sql=sql,
        elapsed_s=elapsed,
    )


def check_presence(
    node_uid: str,
    key_column: str,
    value: str,
    *,
    project: DbtProject,
    manifest: dict[str, Any] | None = None,
    quote_style: str = "adapter",
    timeout: int = 120,
) -> bool:
    """Beslutning A. Signaturen holdes stabil; innmaten kan byttes til
    dbtRunner eller direkte adapter-kall uten at kallere endres.
    """
    return check_presence_detailed(
        node_uid,
        key_column,
        value,
        project=project,
        manifest=manifest,
        quote_style=quote_style,
        timeout=timeout,
    ).present


def check_presence_guarded(
    node_uid: str,
    key_column: str,
    value: str,
    *,
    project: DbtProject,
    manifest: dict[str, Any],
    catalog: dict[str, Any] | None,
    policy: TraversalPolicy = TraversalPolicy(),
    quote_style: str = "adapter",
    timeout: int = 120,
) -> GuardedResult:
    """Presence-sjekk med catalog-validering foran (architectur.md §4).

    Dette er funksjonen traverseringen i steg 3 skal kalle. Den kaster ikke
    på spørringsfeil, men returnerer skipped_query_error, slik at én uleselig
    node ikke river ned hele traverseringen. Verktøyfeil på metadatanivå
    kastes fortsatt.
    """
    ref = classify_node(manifest, catalog, node_uid, key_column, policy)

    if not ref.checkable:
        status = (
            "skipped_missing_key_column"
            if ref.skip_reason == "missing_key_column"
            else "unknown"
        )
        return GuardedResult(
            node_uid=node_uid,
            name=ref.name,
            status=status,
            relation=ref.relation,
            detail=ref.skip_reason,
        )

    sql = build_presence_sql(ref.relation, key_column, value, quote_style)
    try:
        stdout, elapsed = run_dbt_show(project, sql, timeout=timeout)
        count = parse_count(stdout)
    except RowtraceError as exc:
        return GuardedResult(
            node_uid=node_uid,
            name=ref.name,
            status="skipped_query_error",
            relation=ref.relation,
            sql=sql,
            key_column_type=ref.key_column_type,
            detail=str(exc)[:500],
        )

    return GuardedResult(
        node_uid=node_uid,
        name=ref.name,
        status="present" if count > 0 else "absent",
        count=count,
        relation=ref.relation,
        sql=sql,
        elapsed_s=elapsed,
        key_column_type=ref.key_column_type,
    )
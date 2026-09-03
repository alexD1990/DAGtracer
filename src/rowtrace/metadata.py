"""Steg 2 — metadata-lag (architectur.md §3 lag 1, §4).

Ansvar:
  - lese manifest.json og catalog.json
  - oversette kortnavn til node-UID (beslutning B: UID er primærnøkkel internt)
  - finne direkte foreldre for en node
  - validere at nøkkelkolonnen finnes i noden før presence sjekkes

Traverseringspolicy (avklart i steg 2): grensen for hva som kan sjekkes går
ikke ved resource_type, men ved om noden har en spørrbar relasjon. Seeds,
sources og snapshots har relation_name og sjekkes som modeller. Efemere
modeller har det ikke — de hoppes over i sjekk, men traverseres gjennom.
"""

from __future__ import annotations

from pathlib import Path
import json
from dataclasses import dataclass, field
from typing import Any, Iterable

from .project import DbtProject, MetadataError

# --------------------------------------------------------------------------
# Policy
# --------------------------------------------------------------------------

TRAVERSABLE_TYPES = ("model", "seed", "source", "snapshot")

#: Noder som aldri har foreldre. Traversering stopper her uansett policy.
TERMINAL_TYPES = ("seed", "source")


@dataclass(frozen=True)
class TraversalPolicy:
    """Hva traverseringen får følge inn i.

    Standardene er 'på' fordi feilmodusen ved å inkludere en node er synlig
    (én ekstra spørring, eventuelt en skipped_query_error i rapporten), mens
    feilmodusen ved å utelate den er et stille avkortet svar.

    include_sources kan slås av mot warehouse der en usfiltrert count mot en
    rå kildetabell er dyr (BigQuery, Snowflake). Blir --no-source-checks i
    steg 8.
    """

    include_seeds: bool = True
    include_sources: bool = True
    include_snapshots: bool = True


# --------------------------------------------------------------------------
# Lasting
# --------------------------------------------------------------------------


def _load_json(path, what: str) -> dict[str, Any]:
    if not path.is_file():
        hint = (
            "Kjør 'dbt compile' eller 'dbt build' i prosjektet."
            if what == "manifest"
            else "Kjør 'dbt docs generate' i prosjektet."
        )
        raise MetadataError(f"Fant ikke {what}: {path}. {hint}")
    try:
        with path.open(encoding="utf-8") as fh:
            return json.load(fh)
    except json.JSONDecodeError as exc:
        raise MetadataError(f"Kunne ikke parse {what} {path}: {exc}") from exc


def load_manifest(project: DbtProject) -> dict[str, Any]:
    return _load_json(project.manifest_file, "manifest")


def load_catalog(project: DbtProject, *, required: bool = True) -> dict[str, Any] | None:
    """Catalog er valgfri i den forstand at presence fortsatt virker uten den.

    Uten catalog kan vi ikke skille 'kolonnen finnes ikke' fra 'vi vet ikke'.
    Kallere som tåler det sender required=False og får None.
    """
    if not project.catalog_file.is_file():
        if required:
            raise MetadataError(
                f"Fant ikke catalog: {project.catalog_file}. "
                "Kjør 'dbt docs generate' i prosjektet."
            )
        return None
    return _load_json(project.catalog_file, "catalog")


# --------------------------------------------------------------------------
# Node-oppslag
# --------------------------------------------------------------------------


def resource_type_of(node_uid: str) -> str:
    """Utled ressurstype fra UID-prefikset. dbt garanterer formen
    '<type>.<pakke>.<navn>' (sources har fire ledd).
    """
    return node_uid.split(".", 1)[0]


def lookup_node(manifest: dict[str, Any], node_uid: str) -> dict[str, Any]:
    """Sources ligger i manifest['sources'], ikke i manifest['nodes'].

    Dette er en enkel, men lett oversett detalj: seeds og snapshots ligger
    under 'nodes' sammen med modellene, sources gjør det ikke.
    """
    bucket = "sources" if resource_type_of(node_uid) == "source" else "nodes"
    node = manifest.get(bucket, {}).get(node_uid)
    if node is None:
        raise MetadataError(f"Node '{node_uid}' finnes ikke i manifestet ({bucket}).")
    return node


def find_node(
    manifest: dict[str, Any],
    name: str,
    resource_types: Iterable[str] = ("model",),
) -> str:
    """Beslutning B: eneste sted kortnavn -> UID skjer.

    Er 'name' allerede en UID, valideres den bare. Tvetydige kortnavn
    (samme modellnavn i flere pakker) avvises framfor å gjettes på.
    """
    if resource_type_of(name) in TRAVERSABLE_TYPES:
        lookup_node(manifest, name)  # kaster hvis ukjent
        return name

    wanted = set(resource_types)
    matches: list[str] = []
    for bucket in ("nodes", "sources"):
        for uid, node in manifest.get(bucket, {}).items():
            if node.get("resource_type") in wanted and node.get("name") == name:
                matches.append(uid)

    if not matches:
        raise MetadataError(
            f"Fant ingen {'/'.join(sorted(wanted))} med navn eller UID '{name}'."
        )
    if len(matches) > 1:
        raise MetadataError(
            f"Navnet '{name}' er tvetydig (flere pakker): "
            f"{', '.join(sorted(matches))}. Oppgi full node-UID."
        )
    return matches[0]


def is_ephemeral(node: dict[str, Any]) -> bool:
    return node.get("config", {}).get("materialized") == "ephemeral"


def relation_for(manifest: dict[str, Any], node_uid: str) -> str:
    """Ferdig kvalifisert og quotet relasjonsnavn fra dbt selv.

    Vi bygger den aldri fra database/schema/alias — da ville vi tatt på oss
    dialekt- og quoting-ansvar dbt allerede har løst.
    """
    node = lookup_node(manifest, node_uid)
    relation = node.get("relation_name")
    if not relation:
        materialized = node.get("config", {}).get("materialized")
        raise MetadataError(
            f"Node '{node_uid}' mangler relation_name (materialisering={materialized!r}). "
            "Efemere modeller kan ikke presence-sjekkes direkte."
        )
    return relation


def display_name(manifest: dict[str, Any], node_uid: str) -> str:
    return lookup_node(manifest, node_uid).get("name", node_uid)


# --------------------------------------------------------------------------
# Catalog: kolonnevalidering (architectur.md §4)
# --------------------------------------------------------------------------

#: Utfall av catalog-oppslaget for en node.
#:   ok            - noden finnes i catalog og har nøkkelkolonnen
#:   column_missing- noden finnes i catalog, kolonnen gjør ikke -> hopp over
#:   node_absent   - noden finnes ikke i catalog (stale/ikke bygget) -> sjekk likevel
#:   no_catalog    - ingen catalog lastet i det hele tatt -> sjekk likevel
CATALOG_OK = "ok"
CATALOG_COLUMN_MISSING = "column_missing"
CATALOG_NODE_ABSENT = "node_absent"
CATALOG_NO_CATALOG = "no_catalog"


def catalog_entry(catalog: dict[str, Any] | None, node_uid: str) -> dict[str, Any] | None:
    if catalog is None:
        return None
    bucket = "sources" if resource_type_of(node_uid) == "source" else "nodes"
    return catalog.get(bucket, {}).get(node_uid)


def key_column_status(
    catalog: dict[str, Any] | None, node_uid: str, key_column: str
) -> tuple[str, str | None]:
    """Returnerer (status, resolved_type).

    Asymmetrien er bevisst: 'noden finnes i catalog, men kolonnen gjør ikke'
    er et sikkert negativ og gir skipped_missing_key_column. 'Noden finnes
    ikke i catalog' er derimot fravær av kunnskap — typisk fordi
    dbt docs generate ikke er kjørt etter siste build — og skal ikke stille
    kutte noden ut av traverseringen. Da sjekker vi og legger på en advarsel.

    Kolonnenavn matches case-insensitivt fordi catalog bevarer warehousets
    casing (DuckDB små, Snowflake store).
    """
    if catalog is None:
        return CATALOG_NO_CATALOG, None

    entry = catalog_entry(catalog, node_uid)
    if entry is None:
        return CATALOG_NODE_ABSENT, None

    columns = entry.get("columns", {}) or {}
    for col_name, col in columns.items():
        if col_name.lower() == key_column.lower():
            return CATALOG_OK, (col.get("type") if isinstance(col, dict) else None)
    return CATALOG_COLUMN_MISSING, None


# --------------------------------------------------------------------------
# Foreldre-oppslag
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class NodeRef:
    """Én node slik traverseringslaget trenger å se den.

    skip_reason-verdiene er valgt for å kunne mappes rett til presence-enumet
    i architectur.md §12.4 når outputkontrakten låses i steg 7:
      missing_key_column -> skipped_missing_key_column
    """

    node_uid: str
    name: str
    resource_type: str
    relation: str | None
    checkable: bool
    traverse_through: bool
    terminal: bool
    skip_reason: str | None = None
    key_column_type: str | None = None
    catalog_status: str = CATALOG_NO_CATALOG
    warnings: tuple[str, ...] = field(default_factory=tuple)


def classify_node(
    manifest: dict[str, Any],
    catalog: dict[str, Any] | None,
    node_uid: str,
    key_column: str,
    policy: TraversalPolicy = TraversalPolicy(),
) -> NodeRef:
    """Avgjør om noden kan presence-sjekkes, og om traverseringen skal
    fortsette gjennom den. Gjør ingen spørringer.
    """
    rtype = resource_type_of(node_uid)
    node = lookup_node(manifest, node_uid)
    name = node.get("name", node_uid)
    terminal = rtype in TERMINAL_TYPES
    warnings: list[str] = []

    def _skip(reason: str, *, through: bool = False, rel: str | None = None) -> NodeRef:
        return NodeRef(
            node_uid=node_uid,
            name=name,
            resource_type=rtype,
            relation=rel,
            checkable=False,
            traverse_through=through,
            terminal=terminal,
            skip_reason=reason,
            catalog_status=CATALOG_NO_CATALOG if catalog is None else CATALOG_NODE_ABSENT,
            warnings=tuple(warnings),
        )

    if rtype not in TRAVERSABLE_TYPES:
        return _skip("unsupported_resource_type")
    if rtype == "seed" and not policy.include_seeds:
        return _skip("excluded_by_policy")
    if rtype == "source" and not policy.include_sources:
        return _skip("excluded_by_policy")
    if rtype == "snapshot" and not policy.include_snapshots:
        return _skip("excluded_by_policy", through=True)

    # Efemer: ingen relasjon å spørre mot, men foreldrene er fortsatt
    # relevante. Traverser gjennom uten å sjekke.
    if rtype == "model" and is_ephemeral(node):
        return _skip("ephemeral", through=True)

    relation = node.get("relation_name")
    if not relation:
        return _skip("missing_relation", through=not terminal)

    status, coltype = key_column_status(catalog, node_uid, key_column)
    if status == CATALOG_COLUMN_MISSING:
        return NodeRef(
            node_uid=node_uid,
            name=name,
            resource_type=rtype,
            relation=relation,
            checkable=False,
            traverse_through=not terminal,
            terminal=terminal,
            skip_reason="missing_key_column",
            catalog_status=status,
            warnings=tuple(warnings),
        )
    if status == CATALOG_NODE_ABSENT:
        warnings.append(
            f"{name}: ikke i catalog.json — kolonnevalidering hoppet over. "
            "Kjør 'dbt docs generate' hvis dette er uventet."
        )
    elif status == CATALOG_NO_CATALOG:
        warnings.append(f"{name}: ingen catalog lastet — kolonnevalidering hoppet over.")

    return NodeRef(
        node_uid=node_uid,
        name=name,
        resource_type=rtype,
        relation=relation,
        checkable=True,
        traverse_through=not terminal,
        terminal=terminal,
        skip_reason=None,
        key_column_type=coltype,
        catalog_status=status,
        warnings=tuple(warnings),
    )


def parent_uids(manifest: dict[str, Any], node_uid: str) -> list[str]:
    """Rå depends_on.nodes, uten filtrering. Rekkefølge fra manifestet bevares."""
    node = lookup_node(manifest, node_uid)
    return list(node.get("depends_on", {}).get("nodes", []))


def parents_of(
    manifest: dict[str, Any],
    catalog: dict[str, Any] | None,
    node_uid: str,
    key_column: str,
    policy: TraversalPolicy = TraversalPolicy(),
) -> list[NodeRef]:
    """Direkte foreldre, klassifisert. Steg 2 gjør kun ett nivå;
    steg 3 kaller denne rekursivt.
    """
    if resource_type_of(node_uid) in TERMINAL_TYPES:
        return []
    return [
        classify_node(manifest, catalog, uid, key_column, policy)
        for uid in parent_uids(manifest, node_uid)
    ]

# --------------------------------------------------------------------------
# Kompilert SQL (steg 5+6: joins.py, filters.py)
# --------------------------------------------------------------------------

#: node['resource_type'] -> undermappe i target/compiled/<package>/<her>/
_COMPILED_SUBDIR = {
    "model": "models",
    "snapshot": "snapshots",
}


def compiled_path_for_node(project: DbtProject, node: dict[str, Any]) -> "Path | None":
    subdir = _COMPILED_SUBDIR.get(node.get("resource_type", ""))
    rel_path = node.get("path")
    package_name = node.get("package_name")
    if subdir is None or not rel_path or not package_name:
        return None
    return project.project_dir / "target" / "compiled" / package_name / subdir / rel_path


def compiled_sql_for_node(
    project: DbtProject, node: dict[str, Any]
) -> tuple[str | None, str | None]:
    """Kompilert SQL for én node.

    Leser fra target/compiled/... på disk fremfor manifest['compiled_code'].
    Grunn (funnet under steg 5): hver 'dbt show --inline' (én per
    presence-sjekk) skriver et ferskt manifest.json som del av kommandoen,
    men kompilerer kun sin egen ad-hoc-spørring — compiled_code for vanlige
    modeller blir borte fra manifest.json på disk igjen etter en
    traversering. Filene i target/compiled/ er upåvirket av dette.

    Returnerer (sql, kilde_note). kilde_note er None ved normal fil-lesning,
    satt til en forklaring ved fallback til manifestet eller ved feil.
    """
    path = compiled_path_for_node(project, node)
    if path is not None and path.is_file():
        try:
            return path.read_text(encoding="utf-8"), None
        except OSError as exc:
            return None, f"Kunne ikke lese {path}: {exc}"

    fallback = node.get("compiled_code")
    if fallback:
        note = (
            f"target/compiled-fil ikke funnet ({path}); brukte "
            "manifest['compiled_code'] i stedet. Kan være stalt hvis "
            "traversering har kjørt siden siste 'dbt compile'."
        )
        return fallback, note

    return None, None
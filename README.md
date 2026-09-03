# dgt — dbt Row-Level Lineage Tracer

Sporer hvor en rad forsvinner oppstrøms i en dbt-DAG, basert kun på
tilstedeværelse (finnes verdien her eller ikke). Verktøyet gjør ingen
vurdering av *hvorfor* — det er opp til deg å lese modellens SQL.

## Bruk
dgt key,value,from [flagg]

## Eksempel:
- dgt orgnr,222,mart_summary --project-dir testproject --profiles-dir testproject
- `key` — kolonnenavnet du sporer på
- `value` — verdien (alltid behandlet som tekst)
- `from` — startmodell (kortnavn eller full node-UID)

## `.dgt.yml` — slipp å gjenta --project-dir/--profiles-dir

Legg en `.dgt.yml`-fil i mappa du kjører `dgt` fra:

```yaml
project_dir: .
profiles_dir: .
```

Da holder det å skrive:
dgt orgnr,222,mart_summary


**Presedens** (høyest til lavest):

1. `--project-dir`/`--profiles-dir` på kommandolinja, hvis oppgitt
2. Verdi fra `.dgt.yml`, hvis fila finnes og nøkkelen er satt der
3. Hardkodet fallback: kun `project_dir` faller tilbake til `.`.
   `profiles_dir` har ingen egen fallback — mangler den overalt, lar
   `dgt` dbt selv finne profilen (`~/.dbt/` eller `DBT_PROFILES_DIR`).

`.dgt.yml` opprettes ikke automatisk — du lager den selv.

## project_dir vs. profiles_dir

Dette spørres ofte om:

- **`project_dir`** er mappa med `dbt_project.yml` og modellene dine —
  selve dbt-prosjektet.
- **`profiles_dir`** er mappa med `profiles.yml` — filen som sier
  *hvordan* dbt kobler til warehouse (credentials, target). Den ligger
  som regel i `~/.dbt/`, men et prosjekt kan ha sin egen lokale
  `profiles.yml` (slik `testproject/` i dette repoet gjør) — da må du
  peke `--profiles-dir` dit eksplisitt, ellers finner ikke dbt riktig
  profil.

## Output

`--output text` (default), `json`, eller `ndjson`.
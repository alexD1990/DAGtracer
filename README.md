# dgt — dbt Row-Level Lineage Tracer

Traces where a row disappears upstream in a dbt DAG, based solely on
presence (does the value exist here or not). The tool makes no
evaluation of *why* — that's up to you to read the model's SQL.

## Usage
dgt key,value,from [flags]

## Example:
- dgt orgnr,222,mart_summary --project-dir testproject --profiles-dir testproject
- `key` — the column name you're tracing on
- `value` — the value (always treated as text)
- `from` — start model (short name or full node UID)

## `.dgt.yml` — avoid repeating --project-dir/--profiles-dir

Place a `.dgt.yml` file in the directory you run `dgt` from:

```yaml
project_dir: .
profiles_dir: .
```

Then it's enough to write:
dgt orgnr,222,mart_summary


**Precedence** (highest to lowest):

1. `--project-dir`/`--profiles-dir` on the command line, if provided
2. Value from `.dgt.yml`, if the file exists and the key is set there
3. Hardcoded fallback: only `project_dir` falls back to `.`.
   `profiles_dir` has no fallback of its own — if missing everywhere, let
   `dgt` dbt itself find the profile (`~/.dbt/` or `DBT_PROFILES_DIR`).

`.dgt.yml` is not created automatically — you create it yourself.

## project_dir vs. profiles_dir

This is often asked about:

- **`project_dir`** is the directory with `dbt_project.yml` and your models —
  the dbt project itself.
- **`profiles_dir`** is the directory with `profiles.yml` — the file that says
  *how* dbt connects to the warehouse (credentials, target). It's usually
  in `~/.dbt/`, but a project can have its own local `profiles.yml`
  (as `testproject/` in this repo does) — then you must
  point `--profiles-dir` there explicitly, otherwise dbt won't find the right
  profile.

## Output

`--output text` (default), `json`, or `ndjson`.
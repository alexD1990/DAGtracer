# dgt — column and value tracing in dbt DAGs

`dgt` starts at one model and follows the dbt DAG upstream and/or downstream to
find where a column — or specific values in it — **appears, disappears or stops**.

```
dgt <start_model> -c <column> [-v VALUE ... | --values-file FILE]
    [-d up|down|both] [--full-trace] [-o text|json|ndjson]
    [--project-dir DIR] [--profiles-dir DIR] [--target NAME]
```

Column tracing reads only dbt's artifacts (`target/manifest.json` and
`target/catalog.json`). Value tracing also runs one small count query per node
through `dbt show`.

## Install

```bash
pip install -e .            # the dgt command
pip install -e ".[dev]"     # plus pytest, jsonschema, dbt-core and dbt-duckdb for the tests
```

Before running dgt, generate the artifacts in the dbt project:

```bash
dbt docs generate           # writes manifest.json and catalog.json
```

## Usage

The examples below are real output from [`testproject/`](testproject/), a small
DuckDB project that ships with the repository.

### 1. Where does a column come from? (column, upstream)

```
$ dgt dim_customers -c last_name
Column 'last_name' (VARCHAR) from dim_customers (model.testproject.dim_customers)
Start status: present

Upstream
   -3 raw_customers        seed      VARCHAR  present
   -2 stg_customers        model     VARCHAR  present
   -1 fct_orders           model     -        absent     <- stop
      int_customers_named  model     VARCHAR  present
      int_customers_slim   model     -        absent     <- stop

Findings (1)
  -3  present_at_source        raw_customers
```

Levels run in data-flow order: upstream is `-N … -1` and downstream is
`+1 … +N`. The start node is level 0. `<- stop` marks a node where the
column's status differs from the start node's, so the branch stops there.
`int_customers_slim` lacks the column, but `dim_customers` gets it from
`int_customers_named`, so the missing branch is shown without being reported
as a finding.

### 2. Where are values lost? (column + values)

```
$ dgt fct_orders -c status -v completed -v returned
Column 'status' (VARCHAR) from fct_orders (model.testproject.fct_orders)
Values: completed, returned
Start status: present; completed=present, returned=absent

Upstream
   -3 raw_orders           seed      VARCHAR  present    completed=present, returned=present
   -2 stg_orders           model     VARCHAR  present    completed=present, returned=present
      stg_payments         model     -        absent     <- stop
   -1 int_order_payments   model     -        unknown (ephemeral)
      int_orders_enriched  model     -        absent     <- stop
      int_orders_valid     model     VARCHAR  present    completed=present, returned=absent

Findings (2)
  -3  value_present_at_source  raw_orders [completed]
  -1  value_dropped            int_orders_valid [returned]
        parents: stg_orders (present; completed=present, returned=present)
```

`int_orders_valid` keeps the `status` column but filters out `'returned'`.
A branch continues while at least one value still has the start node's status.
Use `--values-file` for many values (one per line; blank lines are skipped).

The column type shown comes from the catalog. It explains differences such as
`'0222'` vs `222`. Values are always sent as string literals, and the
warehouse casts them, so on an `INTEGER` column `-v 01` matches the row with
`1`.

### 3. How far does it get? (downstream and both)

```
$ dgt stg_orders -c status -d down
Column 'status' (VARCHAR) from stg_orders (model.testproject.stg_orders)
Start status: present

Downstream
   +1 int_orders_enriched  model     -        absent     <- stop
      int_orders_valid     model     VARCHAR  present
   +2 fct_orders           model     VARCHAR  present
   +3 dim_customers        model     -        absent     <- stop

Findings (2)
  Stops at
    +1  column_dropped           int_orders_enriched
          parents: stg_orders (present)
    +3  column_dropped           dim_customers
          parents: fct_orders (present), int_customers_named (absent), int_customers_slim (absent)
```

`-d both` runs two independent traversals and combines them in one report:

```
$ dgt stg_customers -c last_name -d both
Column 'last_name' (VARCHAR) from stg_customers (model.testproject.stg_customers)
Start status: present

Upstream
   -1 raw_customers        seed      VARCHAR  present
    0 stg_customers        model     VARCHAR  present

Downstream
   +1 customers_snapshot   snapshot  VARCHAR  present
      int_customers_named  model     VARCHAR  present
      int_customers_slim   model     -        absent     <- stop
   +2 dim_customers        model     VARCHAR  present

Findings (4)
  Upstream
    -1  up    present_at_source        raw_customers
  Downstream
    Reaches
      +1  down  present_at_leaf          customers_snapshot
      +2  down  present_at_leaf          dim_customers
    Stops at
      +1  down  column_dropped           int_customers_slim
            parents: stg_customers (present)
```

Downstream text output groups findings under **Reaches** (leaves), **Stops at**
(drops), **Appears at** and **Absent at leaf**. The grouping is presentation
only and does not change the findings.

## How it works

**Traversal.** A breadth-first search runs from the start node. It tracks the
column, or each value, as a *key*. A key stays live on a branch while its
status matches the start node's. In value mode both the column status and the
value status must match, so a node without the column ends the branch even if
a value is "absent" in both places. A branch continues while at least one key
is live. `--full-trace` never stops and maps everything reachable.

**Edges.** Upstream follows `parent_map`; seeds and sources are terminal.
Downstream follows `child_map` into models and snapshots only (no tests,
exposures or semantic models).

**Findings.** A finding is a property of a node, judged against **all** of the
node's parents. It does not depend on the edge the traversal arrived by.
- **Upstream:** nodes the traversal continued through are judged.
- **Downstream:** the child where a key changes is judged, because that is
  where the change is.
- **The start node:** judged against its parents only upstream, so `both`
  does not report it twice.
- **Precedence:** the column level comes first. A node without the column
  gets `column_dropped`, and no value query runs there.
- **Grouping:** findings are grouped per (node, cause, uncertain), with the
  affected values listed.

| Cause | Direction | Meaning |
|---|---|---|
| `column_dropped` | up, down | Node lacks the column; at least one known parent has it. |
| `column_appears` | up, down | Node has the column; no known parent has it. |
| `present_at_source` | up | Seed/source with the column (column mode). |
| `column_absent_at_source` | up | Seed/source without the column, reached along a branch where it was absent all the way. |
| `present_at_leaf` | down | Model/snapshot without model/snapshot children that still has the column. |
| `column_absent_at_leaf` | down | Leaf without the column, reached along a branch where it was absent all the way. |
| `value_dropped` | up, down | Node has the column but not the value; a known parent has the value. |
| `value_appears` | up, down | Node has the value; no known parent has it. |
| `value_present_at_source` | up | Seed/source with the value. |
| `value_absent_at_source` | up | Seed/source with the column but without the value, along an all-absent branch. |
| `value_present_at_leaf` | down | Leaf that still has the value. |
| `value_absent_at_leaf` | down | Leaf without the value, along an all-absent branch. |

The `*_at_source` and `*_at_leaf` findings make "nowhere upstream" and "never
reaches downstream" explicit. When there are no findings at all, the text
output says **No findings** and explains why.

### Unknown and uncertain

A node is `unknown` when dgt cannot know its status:

- `ephemeral`: ephemeral models have no relation and are not in the catalog.
- `not_in_catalog`: the node was never built, or the catalog is out of date.
  dgt prints a warning.
- `query_error`: a value query failed for this node (not the first query; see
  [Exit codes](#exit-codes)). The shortened message is kept, and dgt prints a
  warning.

Unknown nodes are **transparent**. The traversal passes through them, and when
a child is judged, an unknown parent is replaced by its own parents. A finding
that depends on an unknown node is **uncertain** and lists `candidates`, the
nodes where the change may actually happen:

```
$ dgt fct_orders -c total_paid
...
   0  column_appears           fct_orders  (uncertain: fct_orders, int_order_payments)
        parents: int_orders_enriched (absent), int_orders_valid (absent), stg_payments (absent, via int_order_payments)
```

`total_paid` is created in the ephemeral `int_order_payments`, so dgt can
narrow the origin down to those two nodes, but cannot pick between them.

A start node with unknown status is a tool error. For an ephemeral model, dgt
suggests its parents or children. For a node missing from the catalog, it
suggests `dbt docs generate`.

## Options and configuration

| Flag | Default | |
|---|---|---|
| `-c, --column` | required | Column to trace (case-insensitive against the catalog). |
| `-v, --value` | | Value to trace; repeatable. Allowed characters: `A-Z a-z 0-9 _ - . : + /` and space, max 256. |
| `--values-file` | | One value per line. Cannot be combined with `-v`. |
| `-d, --direction` | `up` | `up`, `down` or `both`. |
| `--full-trace` | off | Do not stop at status changes. |
| `-o, --output` | `text` | `text`, `json` or `ndjson`. |
| `--project-dir` | `.` | dbt project directory. |
| `--profiles-dir`, `--target` | dbt's own lookup | Passed to `dbt show`. |

Two flags are hidden from `--help`:
- `--dbt-executable PATH` overrides which dbt is used. By default dgt uses the
  `dbt` next to the running Python, then the one on `PATH`.
- `--dbt-timeout SECONDS` sets the timeout per value query (default 120).

### `.dgt.yml`

An optional `.dgt.yml` in the **current directory** provides defaults:

```yaml
project_dir: analytics/dbt
profiles_dir: ~/.dbt
target: dev
```

Precedence per setting: CLI flag, then `.dgt.yml`, then the default. Only
`project_dir` has a default (`.`). Without `profiles_dir` or `target`, dbt
uses its own lookup. Relative paths resolve against the current directory.
Unknown keys are ignored with a warning (`unknown key 'profile_dir' ignored;
did you mean 'profiles_dir'?`).

## Output formats

### JSON (`-o json`)

The output is one document. Its contract is
[`schema/dgt-output-1.0.schema.json`](schema/dgt-output-1.0.schema.json)
(JSON Schema 2020-12), and the tests validate output against it.

```jsonc
{
  "schema_version": "1.0",
  "tool_version": "0.1.0",
  "status": "partial",          // complete | partial | error
  "run": {
    "start": {"unique_id": "...", "name": "fct_orders", "resource_type": "model",
              "status": "present", "values": {}},
    "column": "total_paid", "column_type": "DECIMAL(38,1)", "values": [],
    "direction": "up", "full_trace": false, "target": null,
    "started_at": "2026-10-28T15:02:43.697Z", "finished_at": "2026-10-28T15:02:43.703Z",
    "stats": {"nodes_visited": 5, "nodes_probed": 5, "value_queries": 0,
              "query_errors": 0, "findings": 1, "uncertain_findings": 1}
  },
  "traversals": {"up": {"nodes": [ /* level, status, values, column_type,
                                      unknown_reason, detail, stopped, reached_from */ ]},
                 "down": null},
  "findings": [ /* direction, level, cause, node, values, uncertain, candidates, parents */ ],
  "warnings": [],
  "error": null
}
```

- **`status`:**
  - `partial` if any finding is uncertain or a value query failed.
  - `error` for tool errors. In that case `run` is `null` and `error.message`
    explains the problem. The document is still written to stdout, and the
    exit code is still 2.
- **`values`:** objects map each value to `present` / `absent` / `unknown`, in
  the order the values were given. They are empty in column mode. Column-level
  findings list every value judged at the node; value-level findings list the
  affected values.
- **Ordering:** deterministic everywhere. Nodes are ordered by level, then
  `unique_id`. Findings follow data-flow order. Two runs differ only in
  `started_at` and `finished_at`.
- **Versioning:** a minor `schema_version` bump only adds fields, and a major
  bump removes or changes them. Consumers should ignore fields they do not
  know.

### NDJSON (`-o ndjson`)

The output is one JSON object per line, emitted in this order: `run_started`,
`node_probed` (per visited node and direction), `finding`, `warning`,
`run_finished` (status, `finished_at`, stats). A tool error produces a single
`run_finished` with `status: "error"`. The event shapes are under
`$defs/ndjson_event` in the same schema.

### Exit codes

| Code | Meaning |
|---|---|
| 0 | The run completed. Findings, uncertainty and warnings do not change the exit code. |
| 2 | Tool error: bad input, missing or inconsistent artifacts, unknown start status, a failure of the **first** value query (it most likely affects every query), or a usage error. |

## Limitations

- **Name-based.** A column is matched by name, case-insensitively. Renamed
  columns (`amount as payment_amount`) are not followed. dgt does not parse
  SQL, so it shows *where* a column changes, not *why*.
- **Depends on the catalog.** Column status comes only from `catalog.json`,
  which reflects the warehouse at the last `dbt docs generate`. dgt warns when
  the catalog is more than 60 seconds older than the manifest. Nodes missing
  from the catalog are `unknown`, never `absent`.
- **Type conversion varies by warehouse.** Values are compared as string
  literals and cast by the warehouse. Whether `'0222'` matches `222`, or how
  dates and decimals compare, depends on the adapter. Check the column type in
  the output.
- **Inconsistent manifests are rejected.** dbt resolves `ref()`/`source()`
  only for nodes created after the parse started, measured by wall-clock time.
  When the system clock jumps backwards during parsing (seen on WSL2), nodes
  end up with empty `depends_on`, and every column would seem to "appear"
  there. dgt detects nodes that have `refs`/`sources` but no dependencies,
  lists them, and exits with code 2, asking for a fresh `dbt parse`.
- **Performance.** Each value query is a separate `dbt show` process, about 2 s
  each, mostly startup time. Queries run sequentially.

## Development

```bash
pip install -e ".[dev]"
pytest
```

The tests build a copy of `testproject/` in a temporary directory, so nothing
is built in place. See [`testproject/README.md`](testproject/README.md) for the
scenarios. The build is retried only when the manifest is inconsistent, as
described above.
`tests/test_dbt_contract.py` checks dbt behavior that dgt relies on:
`dbt --no-write-json show` leaves `target/` untouched.

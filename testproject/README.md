# testproject

DuckDB dbt project used as the dgt test fixture. `profiles.yml` lives here and
points at a relative database path, so dbt must run with `cwd` set to this
directory. Tests build a copy in a temp directory; nothing is built in place.

| Node | Parents |
|---|---|
| `stg_customers` | `raw_customers` (seed) |
| `stg_orders` | `raw_orders` (seed) |
| `stg_payments` | `raw.payments` (source) |
| `int_customers_named`, `int_customers_slim`, `customers_snapshot` | `stg_customers` |
| `int_orders_valid`, `int_orders_enriched` | `stg_orders` |
| `int_order_payments` (ephemeral) | `stg_payments` |
| `fct_orders` | `int_orders_valid`, `int_orders_enriched`, `int_order_payments` |
| `dim_customers` | `int_customers_named`, `int_customers_slim`, `fct_orders` |

| Scenario | Where |
|---|---|
| Column dropped in an intermediate model | `last_name`: `stg_customers` → `int_customers_slim` |
| Column appears in an intermediate model | `order_size`: created in `int_orders_enriched`, flows to `fct_orders` |
| Value dropped, column kept | `status = 'returned'`: removed by the filter in `int_orders_valid` |
| Partial spread downstream | `status` from `stg_orders`: kept in `int_orders_valid`, not in `int_orders_enriched` |
| Diamond where one branch loses the column | `last_name`: `stg_customers` → `int_customers_named` / `int_customers_slim` → `dim_customers` |
| Ephemeral node (status unknown) | `int_order_payments` |
| Source vs. seed | `raw.payments` is a source created by an `on-run-start` hook |

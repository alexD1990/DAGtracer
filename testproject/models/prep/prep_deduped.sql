-- Scenario 444: order_id 1002 is (deliberately) shared between 444 and 555.
-- Dedup partitioned on order_id ensures only one orgnr survives per order_id.
-- "order by orgnr desc" makes 555 win and 444 lose silently.
select order_id, orgnr, status
from (
    select
        order_id,
        orgnr,
        status,
        row_number() over (partition by order_id order by orgnr desc) as rn
    from {{ ref('prep_active_orders') }}
)
where rn = 1
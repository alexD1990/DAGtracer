-- Scenario 222: INNER JOIN dropper kunder uten ordre.
select
    c.orgnr,
    c.customer_name,
    o.order_id,
    o.status
from {{ ref('stg_customers') }} c
inner join {{ ref('stg_orders') }} o
    on c.orgnr = o.orgnr
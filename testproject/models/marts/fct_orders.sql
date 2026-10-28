select
    v.order_id,
    v.customer_id,
    v.order_date,
    v.status,
    v.amount,
    e.order_size,
    p.total_paid
from {{ ref('int_orders_valid') }} as v
inner join {{ ref('int_orders_enriched') }} as e using (order_id)
left join {{ ref('int_order_payments') }} as p using (order_id)

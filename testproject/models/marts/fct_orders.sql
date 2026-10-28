select
    o.order_id,
    o.customer_id,
    o.order_date,
    o.status,
    o.amount,
    p.total_paid
from {{ ref('stg_orders') }} as o
left join {{ ref('int_order_payments') }} as p using (order_id)

-- Keeps the status column but removes the value 'returned'.
select
    order_id,
    customer_id,
    order_date,
    status,
    amount
from {{ ref('stg_orders') }}
where status <> 'returned'

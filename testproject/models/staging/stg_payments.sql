select
    id as payment_id,
    order_id,
    method,
    amount as payment_amount
from {{ source('raw', 'payments') }}

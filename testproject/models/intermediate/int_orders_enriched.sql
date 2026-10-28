-- Introduces order_size; does not carry status.
select
    order_id,
    amount,
    case when amount >= 50 then 'large' else 'small' end as order_size
from {{ ref('stg_orders') }}

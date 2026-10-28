{{ config(materialized='ephemeral') }}

select
    order_id,
    sum(payment_amount) as total_paid
from {{ ref('stg_payments') }}
group by order_id

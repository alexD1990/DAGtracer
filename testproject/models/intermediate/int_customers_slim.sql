-- Drops last_name.
select
    customer_id,
    first_name,
    country
from {{ ref('stg_customers') }}

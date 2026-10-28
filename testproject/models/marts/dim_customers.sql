select
    c.customer_id,
    c.first_name,
    c.last_name,
    c.country,
    count(f.order_id) as order_count
from {{ ref('stg_customers') }} as c
left join {{ ref('fct_orders') }} as f using (customer_id)
group by all

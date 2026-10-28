select
    n.customer_id,
    n.first_name,
    n.last_name,
    s.country,
    count(f.order_id) as order_count
from {{ ref('int_customers_named') }} as n
inner join {{ ref('int_customers_slim') }} as s using (customer_id)
left join {{ ref('fct_orders') }} as f using (customer_id)
group by all

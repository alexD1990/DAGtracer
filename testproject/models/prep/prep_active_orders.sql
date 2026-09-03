-- Scenario 333: WHERE filtrerer bort CANCELLED.
select order_id, orgnr, status
from {{ ref('stg_orders') }}
where status = 'ACTIVE'
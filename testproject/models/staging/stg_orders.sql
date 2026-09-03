select order_id, orgnr, status
from {{ ref('raw_orders') }}
select orgnr, customer_name
from {{ ref('raw_customers') }}
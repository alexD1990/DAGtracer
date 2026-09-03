
  
    
    

    create  table
      "testproject"."main"."prep_customer_orders__dbt_tmp"
  
    as (
      -- Scenario 222: INNER JOIN dropper kunder uten ordre.
select
    c.orgnr,
    c.customer_name,
    o.order_id,
    o.status
from "testproject"."main"."stg_customers" c
inner join "testproject"."main"."stg_orders" o
    on c.orgnr = o.orgnr
    );
  
  
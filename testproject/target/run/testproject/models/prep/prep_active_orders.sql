
  
    
    

    create  table
      "testproject"."main"."prep_active_orders__dbt_tmp"
  
    as (
      -- Scenario 333: WHERE filtrerer bort CANCELLED.
select order_id, orgnr, status
from "testproject"."main"."stg_orders"
where status = 'ACTIVE'
    );
  
  
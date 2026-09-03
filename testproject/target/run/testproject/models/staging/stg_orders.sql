
  
    
    

    create  table
      "testproject"."main"."stg_orders__dbt_tmp"
  
    as (
      select order_id, orgnr, status
from "testproject"."main"."raw_orders"
    );
  
  

  
    
    

    create  table
      "testproject"."main"."stg_customers__dbt_tmp"
  
    as (
      select orgnr, customer_name
from "testproject"."main"."raw_customers"
    );
  
  
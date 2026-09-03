
  
    
    

    create  table
      "testproject"."main"."prep_deduped__dbt_tmp"
  
    as (
      -- Scenario 444: order_id 1002 er (bevisst) delt mellom 444 og 555.
-- Dedup partisjonert på order_id gjør at kun én orgnr overlever per order_id.
-- "order by orgnr desc" gjør at 555 vinner og 444 tapes stille.
select order_id, orgnr, status
from (
    select
        order_id,
        orgnr,
        status,
        row_number() over (partition by order_id order by orgnr desc) as rn
    from "testproject"."main"."prep_active_orders"
)
where rn = 1
    );
  
  
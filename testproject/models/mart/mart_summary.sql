-- Scenario 555: begge foreldre har verdien, INNER JOIN forklarer ikke bortfallet,
-- og EXCEPT er ikke et mønster diagnostikklaget (steg 6) gjenkjenner.
-- Dette skal derfor gi "unexplained", ikke et falskt selvsikkert svar.
with combined as (
    select
        c.orgnr,
        c.customer_name,
        o.order_id
    from {{ ref('prep_customer_orders') }} c
    inner join {{ ref('prep_active_orders') }} o
        on c.orgnr = o.orgnr
)

select * from combined
except
select * from combined where orgnr = '555'
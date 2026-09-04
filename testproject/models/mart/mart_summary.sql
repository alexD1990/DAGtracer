-- Scenario 555: both parents have the value, INNER JOIN doesn't explain the dropout,
-- and EXCEPT is not a pattern the diagnostic layer (step 6) recognizes.
-- This should therefore give "unexplained", not a falsely confident answer.
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
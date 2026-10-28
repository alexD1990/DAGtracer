{% macro create_raw_payments() %}
create schema if not exists raw;
create or replace table raw.payments as
select * from (values
    (1, 1, 'card', 100.0),
    (2, 2, 'card', 50.0),
    (3, 3, 'invoice', 75.0),
    (4, 4, 'cash', 20.0),
    (5, 5, 'card', 60.0)
) as t(id, order_id, method, amount);
{% endmacro %}

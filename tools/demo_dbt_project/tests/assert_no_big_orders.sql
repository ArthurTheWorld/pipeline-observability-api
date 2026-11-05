{{ config(severity='warn') }}
select order_id, amount from {{ ref('stg_orders') }} where amount > 1000

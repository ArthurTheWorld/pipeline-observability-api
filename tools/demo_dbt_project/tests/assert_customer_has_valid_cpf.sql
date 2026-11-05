select order_id, cpf from {{ ref('stg_orders') }} where cpf like '000%'

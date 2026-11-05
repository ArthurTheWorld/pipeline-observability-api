"""Cria dados de demonstração com um caso de cada desfecho de frescor."""

import datetime as dt

import duckdb

con = duckdb.connect("demo.duckdb")
con.execute("create schema if not exists raw")
now = dt.datetime.now(dt.UTC).replace(tzinfo=None)
con.execute(
    "create or replace table raw.fresh_orders "
    "(order_id int, customer_email varchar, cpf varchar, status varchar, amount double, loaded_at timestamp)"
)
con.execute(
    "insert into raw.fresh_orders values (1,'ana@example.com','000.111.222-33','paid',100,?),"
    "(2,'bia@example.com','123.456.789-09','pending',1500,?),(3,'caio@example.com','111.222.333-96','refunded',50,?)",
    [now - dt.timedelta(hours=1)] * 3,
)
con.execute("create or replace table raw.warning_orders (order_id int, loaded_at timestamp)")
con.execute("insert into raw.warning_orders values (1, ?)", [now - dt.timedelta(hours=30)])
con.execute("create or replace table raw.stale_orders (order_id int, loaded_at timestamp)")
con.execute("insert into raw.stale_orders values (1, ?)", [now - dt.timedelta(hours=200)])
print("seed ok")

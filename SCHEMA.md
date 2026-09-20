# SCHEMA.md

## Order Format (produced by Member 1 - data_processing)

| Field | Type | Notes |
|---|---|---|
| order_id | int (uint64) | |
| side | str | 'BUY' or 'SELL' (uppercase) |
| price | float (double) | |
| quantity | int (uint64) | |
| timestamp_ns | int (uint64) | nanoseconds, from time.perf_counter_ns() |

Binary format (struct): "<QB dQQ" — 33 bytes total per order

## Internal Order Format (used by Member 2 - model/OrderBook)

| Field | Type | Notes |
|---|---|---|
| order_id | int | |
| side | str | 'buy' or 'sell' (lowercase) |
| price | float | |
| qty | int | |
| timestamp | int | |

Note: Member 2's `adapt_order()` function in `baseline_matcher.py` 
converts Member 1's format into this one.

## Matched Trade Output Format (produced by OrderBook.trades)

| Field | Type |
|---|---|
| buy_id | int |
| sell_id | int |
| price | float |
| qty | int |
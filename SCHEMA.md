# SCHEMA.md

This document is the shared source of truth for data formats used
across ChronosMatch modules. Update this file whenever a format
changes — before changing the code, if possible — so the whole team
stays in sync.

---

## Week 1 — Order Format (produced by Member 1 - data_processing)

| Field | Type | Notes |
|---|---|---|
| order_id | int (uint64) | |
| side | str | 'BUY' or 'SELL' (uppercase) |
| price | float (double) | |
| quantity | int (uint64) | |
| timestamp_ns | int (uint64) | nanoseconds, from time.perf_counter_ns() |

Binary format (struct): `"<QB dQQ"` — 33 bytes total per order

### Internal Order Format (used by Member 2 - model/OrderBook, pure-Python baseline)

| Field | Type | Notes |
|---|---|---|
| order_id | int | |
| side | str | 'buy' or 'sell' (lowercase) |
| price | float | |
| qty | int | |
| timestamp | int | |

Note: `adapt_order()` in `src/model/baseline_matcher.py` converts
Member 1's format into this one for the pure-Python baseline engine.

### Matched Trade Output Format (produced by OrderBook.trades)

| Field | Type |
|---|---|
| buy_id | int |
| sell_id | int |
| price | float |
| qty | int |

---

## Week 2 — Cython Matching Engine (src/engine/matching_engine.pyx)

### Engine Input — `add_order()`

```python
engine.add_order(order_id, side, price, quantity, timestamp)
```

| Parameter | Type | Notes |
|---|---|---|
| order_id | uint64 | |
| side | uint8 | **0 = BUY, 1 = SELL** (integer constants, not strings — differs from Week 1 baseline) |
| price | double | |
| quantity | uint64 | |
| timestamp | uint64 | nanoseconds |

Constants defined in `matching_engine.pyx`:
```python
BUY = 0
SELL = 1
```

### Engine Output — `get_trades()`

Returns a list of matched trade dicts, same shape as the Week 1 baseline:

```python
[
    {'buy_id': ..., 'sell_id': ..., 'price': ..., 'qty': ...},
    ...
]
```

### Engine Output — `get_order_book_snapshot()`

Returns the current top-of-book state:

```python
[best_bid, best_ask]
```

- `best_bid` — highest resting BUY price (or `None` if no buy orders resting)
- `best_ask` — lowest resting SELL price (or `None` if no sell orders resting)

This is intended for **Member 3's dashboard** to display live Bid/Ask spread.

### Engine Output — `get_order_count()`

Returns total number of orders received (int) — set up by Member 1, unchanged in Week 2.

### Internal Engine Components (not exposed outside the module)

| Component | Purpose |
|---|---|
| `COrder` (C struct) | Raw C-level order representation defined by Member 1 |
| `COrderObj` (cdef class) | Python-visible wrapper around order fields, used to store resting orders in `buy_orders` / `sell_orders` lists (since raw C structs can't be stored in Python lists) |
| `buy_orders` | Resting BUY orders, sorted highest price / earliest timestamp first |
| `sell_orders` | Resting SELL orders, sorted lowest price / earliest timestamp first |
| `trades` | Internal log of all matched trades so far |

---

## Open Items / To Confirm

- [ ] Confirm with Member 1 whether real order data (from `processor.py`) needs to be adapted before calling `add_order()` on the Cython engine, similar to `adapt_order()` in Week 1
- [ ] Confirm with Member 3 whether `get_order_book_snapshot()` and `get_trades()` cover everything the dashboard needs, or if additional fields (e.g., trade timestamp) should be added
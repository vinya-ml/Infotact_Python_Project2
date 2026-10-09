
from libc.stdint cimport uint64_t, uint8_t
from libc.stdlib cimport realloc, free
from libc.math cimport isfinite


# =========================================================
# Order Constants
# =========================================================

cdef uint8_t BUY = 0
cdef uint8_t SELL = 1


# =========================================================
# C-Level Structures
# =========================================================

cdef struct COrder:
    uint64_t order_id
    uint8_t side
    double price
    uint64_t quantity
    uint64_t timestamp


cdef struct CTrade:
    uint64_t buy_id
    uint64_t sell_id
    double price
    uint64_t quantity


# =========================================================
# Cython Matching Engine
# =========================================================

cdef class MatchingEngine:

    cdef:
        uint64_t order_count

        COrder* buy_orders
        COrder* sell_orders

        uint64_t buy_count
        uint64_t sell_count

        uint64_t buy_capacity
        uint64_t sell_capacity

        CTrade* trades
        uint64_t trade_count
        uint64_t trade_capacity

    # =====================================================
    # Initialization
    # =====================================================

    def __cinit__(self):
        self.order_count = 0

        self.buy_orders = NULL
        self.sell_orders = NULL

        self.buy_count = 0
        self.sell_count = 0

        self.buy_capacity = 0
        self.sell_capacity = 0

        self.trades = NULL
        self.trade_count = 0
        self.trade_capacity = 0

    # =====================================================
    # Memory Cleanup
    # =====================================================

    def __dealloc__(self):
        if self.buy_orders != NULL:
            free(self.buy_orders)
            self.buy_orders = NULL

        if self.sell_orders != NULL:
            free(self.sell_orders)
            self.sell_orders = NULL

        if self.trades != NULL:
            free(self.trades)
            self.trades = NULL

    # =====================================================
    # Capacity Management
    # =====================================================

    cdef void _ensure_buy_capacity(self) except *:
        cdef uint64_t new_capacity
        cdef COrder* new_buffer

        if self.buy_count < self.buy_capacity:
            return

        if self.buy_capacity == 0:
            new_capacity = 16
        else:
            if self.buy_capacity > (<uint64_t>-1) // 2:
                raise MemoryError("Buy order capacity overflow")
            new_capacity = self.buy_capacity * 2

        new_buffer = <COrder*>realloc(
            self.buy_orders,
            new_capacity * sizeof(COrder)
        )

        if new_buffer == NULL:
            raise MemoryError("Unable to allocate buy order buffer")

        self.buy_orders = new_buffer
        self.buy_capacity = new_capacity

    cdef void _ensure_sell_capacity(self) except *:
        cdef uint64_t new_capacity
        cdef COrder* new_buffer

        if self.sell_count < self.sell_capacity:
            return

        if self.sell_capacity == 0:
            new_capacity = 16
        else:
            if self.sell_capacity > (<uint64_t>-1) // 2:
                raise MemoryError("Sell order capacity overflow")
            new_capacity = self.sell_capacity * 2

        new_buffer = <COrder*>realloc(
            self.sell_orders,
            new_capacity * sizeof(COrder)
        )

        if new_buffer == NULL:
            raise MemoryError("Unable to allocate sell order buffer")

        self.sell_orders = new_buffer
        self.sell_capacity = new_capacity

    cdef void _ensure_trade_capacity(self) except *:
        cdef uint64_t new_capacity
        cdef CTrade* new_buffer

        if self.trade_count < self.trade_capacity:
            return

        if self.trade_capacity == 0:
            new_capacity = 16
        else:
            if self.trade_capacity > (<uint64_t>-1) // 2:
                raise MemoryError("Trade capacity overflow")
            new_capacity = self.trade_capacity * 2

        new_buffer = <CTrade*>realloc(
            self.trades,
            new_capacity * sizeof(CTrade)
        )

        if new_buffer == NULL:
            raise MemoryError("Unable to allocate trade buffer")

        self.trades = new_buffer
        self.trade_capacity = new_capacity

    # =====================================================
    # Record Trade
    # =====================================================

    cdef void _record_trade(
        self,
        uint64_t buy_id,
        uint64_t sell_id,
        double price,
        uint64_t quantity
    ) except *:

        self._ensure_trade_capacity()

        self.trades[self.trade_count].buy_id = buy_id
        self.trades[self.trade_count].sell_id = sell_id
        self.trades[self.trade_count].price = price
        self.trades[self.trade_count].quantity = quantity

        self.trade_count += 1

    # =====================================================
    # Insert BUY: Highest Price, Earliest Timestamp
    # =====================================================

    cdef void _insert_buy(self, COrder order) except *:
        cdef uint64_t position
        cdef uint64_t i

        self._ensure_buy_capacity()
        position = self.buy_count

        for i in range(self.buy_count):
            if order.price > self.buy_orders[i].price:
                position = i
                break

            if (
                order.price == self.buy_orders[i].price
                and order.timestamp < self.buy_orders[i].timestamp
            ):
                position = i
                break

        for i in range(self.buy_count, position, -1):
            self.buy_orders[i] = self.buy_orders[i - 1]

        self.buy_orders[position] = order
        self.buy_count += 1

    # =====================================================
    # Insert SELL: Lowest Price, Earliest Timestamp
    # =====================================================

    cdef void _insert_sell(self, COrder order) except *:
        cdef uint64_t position
        cdef uint64_t i

        self._ensure_sell_capacity()
        position = self.sell_count

        for i in range(self.sell_count):
            if order.price < self.sell_orders[i].price:
                position = i
                break

            if (
                order.price == self.sell_orders[i].price
                and order.timestamp < self.sell_orders[i].timestamp
            ):
                position = i
                break

        for i in range(self.sell_count, position, -1):
            self.sell_orders[i] = self.sell_orders[i - 1]

        self.sell_orders[position] = order
        self.sell_count += 1

    # =====================================================
    # Match Incoming BUY Against Resting SELL Orders
    # =====================================================

    cdef void _match_buy(self, COrder* order) except *:
        cdef COrder* best_sell
        cdef uint64_t matched_quantity
        cdef uint64_t i

        while (
            order.quantity > 0
            and self.sell_count > 0
            and self.sell_orders[0].price <= order.price
        ):
            best_sell = &self.sell_orders[0]

            if order.quantity < best_sell.quantity:
                matched_quantity = order.quantity
            else:
                matched_quantity = best_sell.quantity

            # Execute at the resting SELL order's price.
            self._record_trade(
                order.order_id,
                best_sell.order_id,
                best_sell.price,
                matched_quantity
            )

            order.quantity -= matched_quantity
            best_sell.quantity -= matched_quantity

            if best_sell.quantity == 0:
                for i in range(1, self.sell_count):
                    self.sell_orders[i - 1] = self.sell_orders[i]

                self.sell_count -= 1

        # Rest any unfilled quantity on the BUY side.
        if order.quantity > 0:
            self._insert_buy(order[0])

    # =====================================================
    # Match Incoming SELL Against Resting BUY Orders
    # =====================================================

    cdef void _match_sell(self, COrder* order) except *:
        cdef COrder* best_buy
        cdef uint64_t matched_quantity
        cdef uint64_t i

        while (
            order.quantity > 0
            and self.buy_count > 0
            and self.buy_orders[0].price >= order.price
        ):
            best_buy = &self.buy_orders[0]

            if order.quantity < best_buy.quantity:
                matched_quantity = order.quantity
            else:
                matched_quantity = best_buy.quantity

            # Execute at the resting BUY order's price.
            self._record_trade(
                best_buy.order_id,
                order.order_id,
                best_buy.price,
                matched_quantity
            )

            order.quantity -= matched_quantity
            best_buy.quantity -= matched_quantity

            if best_buy.quantity == 0:
                for i in range(1, self.buy_count):
                    self.buy_orders[i - 1] = self.buy_orders[i]

                self.buy_count -= 1

        # Rest any unfilled quantity on the SELL side.
        if order.quantity > 0:
            self._insert_sell(order[0])

    # =====================================================
    # Add Order With Input Validation
    # =====================================================

    cpdef uint64_t add_order(
        self,
        uint64_t order_id,
        uint8_t side,
        double price,
        uint64_t quantity,
        uint64_t timestamp
    ):
        cdef COrder order

        if side != BUY and side != SELL:
            raise ValueError("side must be BUY (0) or SELL (1)")

        if not isfinite(price) or price <= 0.0:
            raise ValueError("price must be finite and greater than zero")

        if quantity == 0:
            raise ValueError("quantity must be greater than zero")

        order.order_id = order_id
        order.side = side
        order.price = price
        order.quantity = quantity
        order.timestamp = timestamp

        if self.order_count == (<uint64_t>-1):
            raise OverflowError("Order count limit reached")

        if side == BUY:
            self._match_buy(&order)
        else:
            self._match_sell(&order)

        self.order_count += 1
        return order_id

    # =====================================================
    # Order Count
    # =====================================================

    cpdef uint64_t get_order_count(self):
        return self.order_count

    # =====================================================
    # Trade Count
    # =====================================================

    cpdef uint64_t get_trade_count(self):
        return self.trade_count

    # =====================================================
    # Trade History
    # Python dictionaries are created only on retrieval.
    # =====================================================

    cpdef list get_trades(self):
        cdef list result = []
        cdef uint64_t i

        for i in range(self.trade_count):
            result.append({
                "buy_id": self.trades[i].buy_id,
                "sell_id": self.trades[i].sell_id,
                "price": self.trades[i].price,
                "qty": self.trades[i].quantity
            })

        return result

    # =====================================================
    # Best Bid / Best Ask Snapshot
    # =====================================================

    cpdef list get_order_book_snapshot(self):
        cdef object best_bid = None
        cdef object best_ask = None

        if self.buy_count > 0:
            best_bid = self.buy_orders[0].price

        if self.sell_count > 0:
            best_ask = self.sell_orders[0].price

        return [best_bid, best_ask]

    # =====================================================
    # Order Book Counts
    # =====================================================

    cpdef tuple get_order_book_counts(self):
        return (self.buy_count, self.sell_count)

    # =====================================================
    # Batch Processing - Reduce Python/Cython Call Overhead
    # Each order: (order_id, side, price, quantity, timestamp)
    # =====================================================

    cpdef uint64_t add_orders_batch(self, list orders):
        cdef Py_ssize_t i
        cdef Py_ssize_t n = len(orders)
        cdef object item
        cdef object raw_side
        cdef double price_value
        cdef COrder order
        cdef uint64_t processed = 0

        for i in range(n):
            item = orders[i]

            if len(item) != 5:
                raise ValueError(
                    "Each order must contain 5 values: "
                    "(order_id, side, price, quantity, timestamp)"
                )

            raw_side = item[1]

            if raw_side != BUY and raw_side != SELL:
                raise ValueError("side must be BUY (0) or SELL (1)")

            price_value = float(item[2])
            if not isfinite(price_value) or price_value <= 0.0:
                raise ValueError("price must be finite and greater than zero")

            order.order_id = item[0]
            order.side = <uint8_t>raw_side
            order.price = price_value
            order.quantity = item[3]
            order.timestamp = item[4]

            if order.quantity == 0:
                raise ValueError("quantity must be greater than zero")

            if self.order_count == (<uint64_t>-1):
                raise OverflowError("Order count limit reached")

            if order.side == BUY:
                self._match_buy(&order)
            else:
                self._match_sell(&order)

            self.order_count += 1
            processed += 1

        return processed

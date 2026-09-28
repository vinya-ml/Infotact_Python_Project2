from libc.stdint cimport uint64_t
from libc.stdint cimport int64_t
from libc.stdint cimport uint8_t


# ---------------------------------------------------------
# Order Constants
# ---------------------------------------------------------

cdef uint8_t BUY = 0
cdef uint8_t SELL = 1


# ---------------------------------------------------------
# C-level Order Structure
# ---------------------------------------------------------

cdef struct COrder:
    uint64_t order_id
    uint8_t side
    double price
    uint64_t quantity
    uint64_t timestamp


# ---------------------------------------------------------
# Order wrapper (Python-visible object used inside the book)
# ---------------------------------------------------------
# COrder (above) is a raw C struct and cannot be stored directly
# inside a Python list. This lightweight cdef class wraps the same
# fields so resting orders can be kept in the buy/sell books while
# still using fast, statically-typed C attributes internally.

cdef class COrderObj:
    cdef public uint64_t order_id
    cdef public uint8_t side
    cdef public double price
    cdef public uint64_t quantity
    cdef public uint64_t timestamp

    def __cinit__(self, uint64_t order_id, uint8_t side, double price,
                  uint64_t quantity, uint64_t timestamp):
        self.order_id = order_id
        self.side = side
        self.price = price
        self.quantity = quantity
        self.timestamp = timestamp


# ---------------------------------------------------------
# Cython Matching Engine
# ---------------------------------------------------------

cdef class MatchingEngine:

    cdef:
        uint64_t order_count
        list buy_orders    # resting BUY orders (highest price, earliest time first)
        list sell_orders   # resting SELL orders (lowest price, earliest time first)
        list trades        # matched trade log

    def __cinit__(self):
        """
        Initialize the Cython matching engine.
        """
        self.order_count = 0
        self.buy_orders = []
        self.sell_orders = []
        self.trades = []

    cpdef add_order(
        self,
        uint64_t order_id,
        uint8_t side,
        double price,
        uint64_t quantity,
        uint64_t timestamp
    ):
        """
        Receive an order from the preprocessing / IPC layer and
        run it through the Price-Time Priority matching logic.

        Member 1 prepared the engine input (this signature + COrder struct).
        Member 2 (this section) implements the actual order-book matching.
        """

        cdef COrder order

        # Store order data in C-level structure (kept from Member 1's version)
        order.order_id = order_id
        order.side = side
        order.price = price
        order.quantity = quantity
        order.timestamp = timestamp

        self.order_count += 1

        # Wrap the order so it can be matched / rested in the book
        cdef COrderObj wrapped = COrderObj(order_id, side, price, quantity, timestamp)

        if side == BUY:
            self._match_buy(wrapped)
        else:
            self._match_sell(wrapped)

        return order.order_id

    cdef void _match_buy(self, COrderObj order):
        """
        Match an incoming BUY order against resting SELL orders.
        Lowest price, earliest timestamp is matched first.
        """
        self.sell_orders.sort(key=lambda o: (o.price, o.timestamp))

        while order.quantity > 0 and self.sell_orders and self.sell_orders[0].price <= order.price:
            best_sell = self.sell_orders[0]
            matched_qty = min(order.quantity, best_sell.quantity)

            self.trades.append({
                'buy_id': order.order_id,
                'sell_id': best_sell.order_id,
                'price': best_sell.price,
                'qty': matched_qty
            })

            order.quantity -= matched_qty
            best_sell.quantity -= matched_qty

            if best_sell.quantity == 0:
                self.sell_orders.pop(0)

        if order.quantity > 0:
            self.buy_orders.append(order)

    cdef void _match_sell(self, COrderObj order):
        """
        Match an incoming SELL order against resting BUY orders.
        Highest price, earliest timestamp is matched first.
        """
        self.buy_orders.sort(key=lambda o: (-o.price, o.timestamp))

        while order.quantity > 0 and self.buy_orders and self.buy_orders[0].price >= order.price:
            best_buy = self.buy_orders[0]
            matched_qty = min(order.quantity, best_buy.quantity)

            self.trades.append({
                'buy_id': best_buy.order_id,
                'sell_id': order.order_id,
                'price': best_buy.price,
                'qty': matched_qty
            })

            order.quantity -= matched_qty
            best_buy.quantity -= matched_qty

            if best_buy.quantity == 0:
                self.buy_orders.pop(0)

        if order.quantity > 0:
            self.sell_orders.append(order)

    cpdef uint64_t get_order_count(self):
        """
        Return total number of orders received.
        """
        return self.order_count

    cpdef list get_trades(self):
        """
        Return all matched trades so far.
        Used by Member 3's dashboard to display live matches.
        """
        return self.trades

    cpdef list get_order_book_snapshot(self):
        """
        Return the current top-of-book state for the dashboard:
        best bid (highest buy price) and best ask (lowest sell price).
        """
        best_bid = self.buy_orders[0].price if self.buy_orders else None
        best_ask = self.sell_orders[0].price if self.sell_orders else None
        return [best_bid, best_ask]
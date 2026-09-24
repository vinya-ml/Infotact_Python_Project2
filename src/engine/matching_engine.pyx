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
# Cython Matching Engine
# ---------------------------------------------------------

cdef class MatchingEngine:

    cdef:
        uint64_t order_count

    def __cinit__(self):
        """
        Initialize the Cython matching engine.
        """
        self.order_count = 0

    cpdef add_order(
        self,
        uint64_t order_id,
        uint8_t side,
        double price,
        uint64_t quantity,
        uint64_t timestamp
    ):
        """
        Receive an order from the preprocessing / IPC layer.

        Member 1 only prepares the engine input.
        Actual order-book matching will be implemented
        by Member 2.
        """

        cdef COrder order

        # Store order data in C-level structure
        order.order_id = order_id
        order.side = side
        order.price = price
        order.quantity = quantity
        order.timestamp = timestamp

        self.order_count += 1

        return order.order_id

    cpdef uint64_t get_order_count(self):
        """
        Return total number of orders received.
        """
        return self.order_count

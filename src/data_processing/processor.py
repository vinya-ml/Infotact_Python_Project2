import struct
import time
from dataclasses import dataclass


# Binary format:
# order_id  : unsigned 64-bit integer
# side      : 1 byte (0 = BUY, 1 = SELL)
# price     : 64-bit float
# quantity  : unsigned 64-bit integer
# timestamp : unsigned 64-bit integer
#
# Total record size = 8 + 1 + 8 + 8 + 8 = 33 bytes

ORDER_FORMAT = "<QB dQQ"
ORDER_SIZE = struct.calcsize(ORDER_FORMAT)


@dataclass
class Order:
    """Represents a single market order."""

    order_id: int
    side: str
    price: float
    quantity: int
    timestamp_ns: int


class DataProcessor:
    """Prepares ChronosMatch orders for zero-copy IPC."""

    BUY = 0
    SELL = 1

    def __init__(self):
        self.processed_count = 0

    # -------------------------------------------------
    # CREATE ORDER
    # -------------------------------------------------

    def create_order(
        self,
        order_id: int,
        side: str,
        price: float,
        quantity: int,
        timestamp_ns: int | None = None,
    ) -> Order:
        """Create and validate a trading order."""

        if timestamp_ns is None:
            timestamp_ns = time.perf_counter_ns()

        side = side.upper()

        if side not in ("BUY", "SELL"):
            raise ValueError("side must be BUY or SELL")

        if order_id < 0:
            raise ValueError("order_id must be non-negative")

        if price <= 0:
            raise ValueError("price must be greater than zero")

        if quantity <= 0:
            raise ValueError("quantity must be greater than zero")

        return Order(
            order_id=order_id,
            side=side,
            price=price,
            quantity=quantity,
            timestamp_ns=timestamp_ns,
        )

    # -------------------------------------------------
    # SERIALIZE ORDER
    # -------------------------------------------------

    def pack_order(self, order: Order) -> bytes:
        """
        Convert an Order into a fixed-size binary record.

        This binary record can be written directly
        into the mmap ring buffer.
        """

        side_value = self.BUY if order.side == "BUY" else self.SELL

        packed = struct.pack(
            ORDER_FORMAT,
            order.order_id,
            side_value,
            order.price,
            order.quantity,
            order.timestamp_ns,
        )

        self.processed_count += 1

        return packed

    # -------------------------------------------------
    # DESERIALIZE ORDER
    # -------------------------------------------------

    def unpack_order(self, data: bytes) -> Order:
        """Convert a binary record back into an Order."""

        if len(data) != ORDER_SIZE:
            raise ValueError(
                f"Invalid order size: expected {ORDER_SIZE} bytes, "
                f"got {len(data)} bytes"
            )

        order_id, side_value, price, quantity, timestamp_ns = struct.unpack(
            ORDER_FORMAT,
            data,
        )

        side = "BUY" if side_value == self.BUY else "SELL"

        return Order(
            order_id=order_id,
            side=side,
            price=price,
            quantity=quantity,
            timestamp_ns=timestamp_ns,
        )

    # -------------------------------------------------
    # PROCESS ORDER
    # -------------------------------------------------

    def process_order(
        self,
        order_id: int,
        side: str,
        price: float,
        quantity: int,
    ) -> bytes:
        """
        Create an order and immediately convert it
        into binary data for the IPC layer.
        """

        order = self.create_order(
            order_id=order_id,
            side=side,
            price=price,
            quantity=quantity,
        )

        return self.pack_order(order)

    # -------------------------------------------------
    # BATCH PROCESSING
    # -------------------------------------------------

    def process_orders(self, orders: list[dict]) -> list[bytes]:
        """Convert multiple orders into binary records."""

        packed_orders = []

        for order_data in orders:
            packed = self.process_order(
                order_id=order_data["order_id"],
                side=order_data["side"],
                price=order_data["price"],
                quantity=order_data["quantity"],
            )

            packed_orders.append(packed)

        return packed_orders

    # -------------------------------------------------
    # INFORMATION
    # -------------------------------------------------

    @staticmethod
    def record_size() -> int:
        """Return the fixed binary size of one order."""

        return ORDER_SIZE


# -----------------------------------------------------
# TEST
# -----------------------------------------------------

if __name__ == "__main__":

    processor = DataProcessor()

    order = processor.create_order(
        order_id=100001,
        side="BUY",
        price=185.42,
        quantity=100,
    )

    binary_order = processor.pack_order(order)

    print("ChronosMatch Data Processor")
    print("---------------------------")
    print(f"Order ID      : {order.order_id}")
    print(f"Side          : {order.side}")
    print(f"Price         : {order.price}")
    print(f"Quantity      : {order.quantity}")
    print(f"Timestamp (ns): {order.timestamp_ns}")
    print(f"Binary size   : {len(binary_order)} bytes")
    print(f"Record size   : {ORDER_SIZE} bytes")

    restored_order = processor.unpack_order(binary_order)

    print("\nDecoded Order")
    print("-------------")
    print(restored_order)

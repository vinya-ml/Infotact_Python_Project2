"""
Basic correctness test for the mmap ring buffer (src/data_processing/ipc_buffer.py).

This proves write/read works correctly within a single process before
moving on to the real two-process proof required for the Mid-Project Review.

Run from the project root:
    python test_ipc_buffer.py
"""

import os

from src.data_processing.ipc_buffer import open_buffer
from src.data_processing.processor import DataProcessor

BUFFER_PATH = "test_orders.mmap"


def cleanup():
    if os.path.exists(BUFFER_PATH):
        os.remove(BUFFER_PATH)


def test_write_and_read_single_order():
    cleanup()
    processor = DataProcessor()

    with open_buffer(BUFFER_PATH, num_slots=10, create=True) as buf:
        order = processor.create_order(order_id=1, side="BUY", price=100.5, quantity=50)
        packed = processor.pack_order(order)

        buf.write(packed)
        assert buf.pending_count() == 1

        result = buf.read()
        assert result == packed

        restored = processor.unpack_order(result)
        assert restored.order_id == 1
        assert restored.side == "BUY"
        assert restored.price == 100.5
        assert restored.quantity == 50

        assert buf.pending_count() == 0

    print("test_write_and_read_single_order passed")
    cleanup()


def test_multiple_orders_fifo_order():
    cleanup()
    processor = DataProcessor()

    with open_buffer(BUFFER_PATH, num_slots=10, create=True) as buf:
        for i in range(5):
            order = processor.create_order(order_id=i, side="SELL", price=100 + i, quantity=10)
            buf.write(processor.pack_order(order))

        assert buf.pending_count() == 5

        results = []
        for _ in range(5):
            data = buf.read()
            results.append(processor.unpack_order(data))

        # Orders must come back in the same order they were written (FIFO)
        for i, order in enumerate(results):
            assert order.order_id == i

        assert buf.pending_count() == 0

    print("test_multiple_orders_fifo_order passed")
    cleanup()


def test_ring_wraparound():
    cleanup()
    processor = DataProcessor()

    # Small buffer (3 slots) forces wraparound after a few writes
    with open_buffer(BUFFER_PATH, num_slots=3, create=True) as buf:
        for i in range(3):
            order = processor.create_order(order_id=i, side="BUY", price=100, quantity=1)
            buf.write(processor.pack_order(order))

        # Drain the buffer
        for _ in range(3):
            buf.read()

        # Write again - this will wrap around to slot 0, 1, 2 again
        for i in range(10, 13):
            order = processor.create_order(order_id=i, side="BUY", price=100, quantity=1)
            buf.write(processor.pack_order(order))

        results = []
        for _ in range(3):
            data = buf.read()
            results.append(processor.unpack_order(data))

        assert [o.order_id for o in results] == [10, 11, 12]

    print("test_ring_wraparound passed")
    cleanup()


def test_read_when_empty_returns_none():
    cleanup()
    with open_buffer(BUFFER_PATH, num_slots=5, create=True) as buf:
        assert buf.read() is None

    print("test_read_when_empty_returns_none passed")
    cleanup()


if __name__ == "__main__":
    test_write_and_read_single_order()
    test_multiple_orders_fifo_order()
    test_ring_wraparound()
    test_read_when_empty_returns_none()
    print("\nAll IPC buffer tests passed!")
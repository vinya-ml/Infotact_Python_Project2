
"""Consume mmap order records and submit them to the Cython matching engine."""

from src.data_processing.processor import DataProcessor
from src.engine.matching_engine import MatchingEngine
from src.ipc.ring_buffer import MmapRingBuffer


class RingBufferConsumer:
    """Read serialized orders and submit them to MatchingEngine."""

    def __init__(self, ring_buffer, engine=None, processor=None):
        self.ring_buffer = ring_buffer
        self.engine = engine if engine is not None else MatchingEngine()
        self.processor = processor if processor is not None else DataProcessor()
        self.processed_count = 0

    def process_one(self):
        """Process one queued order; return False if the buffer is empty."""
        record = self.ring_buffer.get()

        if record is None:
            return False

        order = self.processor.unpack_order(record)
        side = (
            self.processor.BUY
            if order.side == "BUY"
            else self.processor.SELL
        )

        self.engine.add_order(
            order.order_id,
            side,
            order.price,
            order.quantity,
            order.timestamp_ns,
        )
        self.processed_count += 1
        return True

    def drain(self, max_orders=None):
        """Process queued orders until empty or max_orders is reached."""
        processed = 0

        while max_orders is None or processed < max_orders:
            if not self.process_one():
                break
            processed += 1

        return processed


def main():
    """Small standalone integration demonstration."""
    processor = DataProcessor()

    with MmapRingBuffer("reports/test_ring.mmap", capacity=16) as ring:
        # Clear any leftover records from an earlier demo.
        while ring.get() is not None:
            pass

        consumer = RingBufferConsumer(ring)

        orders = [
            processor.create_order(100001, "BUY", 185.42, 100, 123456),
            processor.create_order(100002, "SELL", 185.42, 40, 123457),
        ]

        for order in orders:
            ring.put(processor.pack_order(order))

        count = consumer.drain()

        print("orders_processed =", count)
        print("engine_order_count =", consumer.engine.get_order_count())
        print("trade_count =", consumer.engine.get_trade_count())
        print("trades =", consumer.engine.get_trades())
        print("order_book_counts =", consumer.engine.get_order_book_counts())


if __name__ == "__main__":
    main()

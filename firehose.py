"""
ChronosMatch Market Firehose (asyncio).

Week 1 requirement: "Build the asyncio script that blasts mock trade
orders per second into the IPC bus."

This script generates randomized BUY/SELL orders and writes them
directly into the shared mmap ring buffer (src/data_processing/ipc_buffer.py),
using processor.py's binary format. It is meant to run as its own
process, with a separate consumer process reading from the same buffer.

Run from the project root:
    python firehose.py --orders 100000 --rate 100000
"""

import argparse
import asyncio
import os
import random
import time

from src.data_processing.ipc_buffer import open_buffer
from src.data_processing.processor import DataProcessor

BUFFER_PATH = "chronosmatch_orders.mmap"


async def generate_and_write_orders(
    buf,
    processor: DataProcessor,
    num_orders: int,
    orders_per_second: int,
    base_price: float = 100.0,
):
    """
    Generate `num_orders` randomized orders and write them into the
    shared buffer, yielding control periodically so this behaves like
    a real asyncio producer rather than a blocking loop.
    """
    batch_size = max(1, orders_per_second // 100)  # yield ~100 times/sec
    written = 0
    start_time = time.perf_counter()

    for i in range(num_orders):
        side = "BUY" if random.random() < 0.5 else "SELL"
        drift = random.gauss(0, 0.8)
        price = round(max(1.0, base_price + drift), 2)

        # Occasional "whale" order (large quantity)
        if random.random() < 0.02:
            quantity = random.randint(500, 2000)
        else:
            quantity = random.randint(1, 100)

        order = processor.create_order(
            order_id=i,
            side=side,
            price=price,
            quantity=quantity,
        )
        packed = processor.pack_order(order)
        buf.write(packed)

        written += 1

        # Yield control periodically to behave like a real async stream
        if written % batch_size == 0:
            await asyncio.sleep(0)

    elapsed = time.perf_counter() - start_time
    rate = num_orders / elapsed if elapsed > 0 else 0
    print(f"\nFirehose finished: {written:,} orders written in {elapsed:.3f}s "
          f"({rate:,.0f} orders/sec)")


async def main(num_orders: int, orders_per_second: int, num_slots: int):
    if os.path.exists(BUFFER_PATH):
        os.remove(BUFFER_PATH)

    buf = open_buffer(BUFFER_PATH, num_slots=num_slots, create=True)
    processor = DataProcessor()

    print(f"ChronosMatch Market Firehose")
    print(f"Writing {num_orders:,} orders into {BUFFER_PATH} ...")
    print("(Start the consumer in another terminal to read them.)\n")

    await generate_and_write_orders(buf, processor, num_orders, orders_per_second)

    buf.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ChronosMatch Market Firehose")
    parser.add_argument("--orders", type=int, default=10_000,
                         help="Total number of orders to generate (default: 10,000)")
    parser.add_argument("--rate", type=int, default=10_000,
                         help="Target orders per second (default: 10,000)")
    parser.add_argument("--slots", type=int, default=None,
                         help="Ring buffer capacity (default: same as --orders)")
    args = parser.parse_args()

    slots = args.slots if args.slots else args.orders

    asyncio.run(main(args.orders, args.rate, slots))
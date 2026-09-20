"""Deterministic mock market firehose for ChronosMatch (Member 3).

Generates reproducible BUY/SELL order dicts compatible with Member 1's
DataProcessor (order_id / side / price / quantity) and Member 2's OrderBook
via benchmark.py's adapter. Seeded RNG keeps tests stable.
"""

from __future__ import annotations

import random
import time


class MockMarketSimulator:
    def __init__(self, seed: int = 42, base_price: float = 100.0) -> None:
        self.seed = seed
        self.base_price = float(base_price)
        self._rng = random.Random(seed)
        self._next_id = 1

    def reset(self, seed: int | None = None) -> None:
        if seed is not None:
            self.seed = seed
        self._rng = random.Random(self.seed)
        self._next_id = 1

    def _next_order(self, whale_prob: float = 0.02) -> dict:
        side = self._rng.choice(["BUY", "SELL"])
        # random-walk price around base
        drift = self._rng.gauss(0, 0.8)
        price = round(max(1.0, self.base_price + drift), 2)
        if self._rng.random() < whale_prob:
            quantity = self._rng.randint(500, 2000)  # whale sweep
        else:
            quantity = self._rng.randint(1, 100)
        order = {
            "order_id": self._next_id,
            "side": side,
            "price": price,
            "quantity": quantity,
        }
        self._next_id += 1
        return order

    def generate(self, n: int, whale_prob: float = 0.02) -> list[dict]:
        return [self._next_order(whale_prob) for _ in range(max(0, n))]

    def stream(self, n: int, whale_prob: float = 0.02):
        for _ in range(max(0, n)):
            yield self._next_order(whale_prob)

    def balanced_book(self, n_each_side: int = 50) -> list[dict]:
        """Overlapping bid/ask ladder that guarantees matches occur."""
        orders: list[dict] = []
        for i in range(n_each_side):
            orders.append({
                "order_id": self._next_id, "side": "BUY",
                "price": round(self.base_price + 1.0 - i * 0.05, 2),
                "quantity": 10,
            })
            self._next_id += 1
            orders.append({
                "order_id": self._next_id, "side": "SELL",
                "price": round(self.base_price - 1.0 + i * 0.05, 2),
                "quantity": 10,
            })
            self._next_id += 1
        # interleave so buys rest first, then sells match
        return orders

    def timestamped(self, n: int, whale_prob: float = 0.02) -> list[dict]:
        orders = self.generate(n, whale_prob)
        base = time.perf_counter_ns()
        for i, o in enumerate(orders):
            o["timestamp_ns"] = base + i
        return orders

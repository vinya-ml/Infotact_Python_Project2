"""Latency, throughput and order-book analytics for ChronosMatch (Member 3).

Works with Member 2's OrderBook via duck-typing so Member 2 code is never
modified. All statistics use the standard library only.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field


def percentile(values: list[float], pct: float) -> float:
    """Return the pct-th percentile (0-100) using linear interpolation."""
    if not values:
        return 0.0
    if len(values) == 1:
        return float(values[0])
    ordered = sorted(values)
    if pct <= 0:
        return float(ordered[0])
    if pct >= 100:
        return float(ordered[-1])
    rank = (pct / 100.0) * (len(ordered) - 1)
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    frac = rank - low
    return float(ordered[low] * (1.0 - frac) + ordered[high] * frac)


class LatencyTracker:
    """Collects per-order end-to-end latencies in nanoseconds."""

    def __init__(self) -> None:
        self.latencies_ns: list[int] = []
        self._start_ns: int | None = None
        self._end_ns: int | None = None

    def start(self, now_ns: int) -> None:
        self._start_ns = now_ns

    def stop(self, now_ns: int) -> None:
        self._end_ns = now_ns

    def add(self, latency_ns: int) -> None:
        self.latencies_ns.append(int(latency_ns))

    def extend(self, latencies: list[int]) -> None:
        self.latencies_ns.extend(int(v) for v in latencies)

    def clear(self) -> None:
        self.latencies_ns.clear()
        self._start_ns = None
        self._end_ns = None

    def __len__(self) -> int:
        return len(self.latencies_ns)

    def stats(self) -> dict:
        """Return count/min/max/mean/p50/p95/p99 in both ns and us."""
        n = len(self.latencies_ns)
        if n == 0:
            base = {"count": 0, "min_ns": 0, "max_ns": 0, "mean_ns": 0.0,
                    "p50_ns": 0.0, "p95_ns": 0.0, "p99_ns": 0.0}
        else:
            base = {
                "count": n,
                "min_ns": int(min(self.latencies_ns)),
                "max_ns": int(max(self.latencies_ns)),
                "mean_ns": float(statistics.fmean(self.latencies_ns)),
                "p50_ns": float(percentile(self.latencies_ns, 50)),
                "p95_ns": float(percentile(self.latencies_ns, 95)),
                "p99_ns": float(percentile(self.latencies_ns, 99)),
            }
        base["min_us"] = base["min_ns"] / 1000.0
        base["max_us"] = base["max_ns"] / 1000.0
        base["mean_us"] = base["mean_ns"] / 1000.0
        base["p50_us"] = base["p50_ns"] / 1000.0
        base["p95_us"] = base["p95_ns"] / 1000.0
        base["p99_us"] = base["p99_ns"] / 1000.0
        base["throughput_ops"] = self.throughput()
        return base

    def throughput(self) -> float:
        """Orders/sec measured from start/stop wall time, 0 if unavailable."""
        if self._start_ns is None or self._end_ns is None:
            return 0.0
        elapsed_s = (self._end_ns - self._start_ns) / 1e9
        if elapsed_s <= 0:
            return 0.0
        return len(self.latencies_ns) / elapsed_s


@dataclass
class BookSnapshot:
    best_bid: float | None = None
    best_ask: float | None = None
    spread: float | None = None
    mid: float | None = None
    bid_depth: int = 0
    ask_depth: int = 0
    bid_qty: int = 0
    ask_qty: int = 0


class OrderBookAnalytics:
    """Read-only analytics over Member 2's OrderBook (no mutation)."""

    @staticmethod
    def _orders(book, side: str) -> list:
        if side == "bid":
            return list(getattr(book, "buy_orders", []))
        return list(getattr(book, "sell_orders", []))

    @staticmethod
    def _price(order) -> float:
        return float(getattr(order, "price"))

    @staticmethod
    def _qty(order) -> int:
        for attr in ("qty", "quantity"):
            if hasattr(order, attr):
                return int(getattr(order, attr))
        return 0

    @classmethod
    def snapshot(cls, book) -> BookSnapshot:
        bids = cls._orders(book, "bid")
        asks = cls._orders(book, "ask")
        best_bid = max((cls._price(o) for o in bids), default=None)
        best_ask = min((cls._price(o) for o in asks), default=None)
        spread = (best_ask - best_bid) if (best_bid is not None and best_ask is not None) else None
        mid = ((best_ask + best_bid) / 2.0) if spread is not None else None
        return BookSnapshot(
            best_bid=best_bid,
            best_ask=best_ask,
            spread=spread,
            mid=mid,
            bid_depth=len(bids),
            ask_depth=len(asks),
            bid_qty=sum(cls._qty(o) for o in bids),
            ask_qty=sum(cls._qty(o) for o in asks),
        )

    @staticmethod
    def depth_by_price(orders: list) -> dict[float, int]:
        depth: dict[float, int] = {}
        for o in orders:
            price = float(getattr(o, "price"))
            qty = int(getattr(o, "qty", getattr(o, "quantity", 0)))
            depth[price] = depth.get(price, 0) + qty
        return dict(sorted(depth.items()))

    @staticmethod
    def vwap(trades: list[dict]) -> float | None:
        total_qty = sum(int(t.get("qty", 0)) for t in trades)
        if total_qty == 0:
            return None
        notional = sum(float(t.get("price", 0.0)) * int(t.get("qty", 0)) for t in trades)
        return notional / total_qty

    @staticmethod
    def trade_stats(trades: list[dict]) -> dict:
        if not trades:
            return {"count": 0, "total_qty": 0, "vwap": None,
                    "min_price": None, "max_price": None}
        prices = [float(t.get("price", 0.0)) for t in trades]
        total_qty = sum(int(t.get("qty", 0)) for t in trades)
        return {
            "count": len(trades),
            "total_qty": total_qty,
            "vwap": OrderBookAnalytics.vwap(trades),
            "min_price": min(prices),
            "max_price": max(prices),
        }

    @staticmethod
    def fill_rate(trades: list[dict], submitted_qty: int) -> float:
        if submitted_qty <= 0:
            return 0.0
        filled = sum(int(t.get("qty", 0)) for t in trades)
        return filled / float(submitted_qty)

    @staticmethod
    def whale_trades(trades: list[dict], threshold_qty: int) -> list[dict]:
        """Trades at/above threshold_qty — highlights 'whale' sweeps."""
        return [t for t in trades if int(t.get("qty", 0)) >= threshold_qty]

    @staticmethod
    def spread_history(snapshots: list[BookSnapshot]) -> list[float | None]:
        return [s.spread for s in snapshots]


@dataclass
class ThroughputResult:
    num_orders: int = 0
    elapsed_s: float = 0.0
    orders_per_sec: float = 0.0
    bytes_processed: int = 0
    mb_per_sec: float = 0.0
    latencies_ns: list[int] = field(default_factory=list)

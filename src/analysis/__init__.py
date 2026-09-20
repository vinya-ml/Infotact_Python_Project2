"""Member 3: Analysis, Visualization & Testing for ChronosMatch.

Integrates Member 1 (src/data_processing: DataProcessor binary serialization)
with Member 2 (src/model: OrderBook price-time priority matching) to provide:

- latency / throughput analysis  -> metrics.py
- order-book + latency visuals   -> visualizer.py (matplotlib + ASCII fallback)
- deterministic market firehose  -> simulator.py
- end-to-end benchmark harness   -> benchmark.py
"""

from .metrics import LatencyTracker, OrderBookAnalytics, percentile
from .visualizer import (
    ascii_histogram,
    render_latency_text,
    render_order_book_text,
    render_trades_text,
)
from .simulator import MockMarketSimulator
from .benchmark import BenchmarkResult, ipc_audit, run_benchmark

__all__ = [
    "LatencyTracker",
    "OrderBookAnalytics",
    "percentile",
    "ascii_histogram",
    "render_latency_text",
    "render_order_book_text",
    "render_trades_text",
    "MockMarketSimulator",
    "BenchmarkResult",
    "ipc_audit",
    "run_benchmark",
]

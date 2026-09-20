"""Terminal + matplotlib visualization for ChronosMatch (Member 3).

- Pure-stdlib ASCII rendering always works (Windows-safe, no curses needed).
- matplotlib plots are optional: functions skip gracefully with a message if
  matplotlib is not installed, otherwise they save PNG files.
"""

from __future__ import annotations

import os


def ascii_histogram(values: list[float], bins: int = 20, width: int = 40) -> str:
    if not values:
        return "(no data)"
    lo, hi = min(values), max(values)
    if lo == hi:
        return f"all values = {lo:.3f} (n={len(values)})\n" + "#" * width
    edges = [lo + (hi - lo) * i / bins for i in range(bins + 1)]
    counts = [0] * bins
    for v in values:
        idx = min(int((v - lo) / (hi - lo) * bins), bins - 1)
        counts[idx] += 1
    peak = max(counts)
    lines = []
    for i, c in enumerate(counts):
        bar = "#" * (int(c / peak * width) if peak else 0)
        lines.append(f"{edges[i]:10.3f}-{edges[i+1]:10.3f} | {bar} ({c})")
    return "\n".join(lines)


def _qty_of(order) -> int:
    return int(getattr(order, "qty", getattr(order, "quantity", 0)))


def render_order_book_text(book, depth: int = 10) -> str:
    buys = sorted(list(getattr(book, "buy_orders", [])),
                  key=lambda o: (-float(o.price), int(getattr(o, "timestamp", getattr(o, "timestamp_ns", 0)))))
    sells = sorted(list(getattr(book, "sell_orders", [])),
                   key=lambda o: (float(o.price), int(getattr(o, "timestamp", getattr(o, "timestamp_ns", 0)))))
    lines = ["=" * 58, "CHRONOSMATCH ORDER BOOK (Member 3 view)", "=" * 58]
    lines.append(f"{'ASKS (sell)':^58}")
    lines.append(f"{'Price':>12} {'Qty':>10} {'OrderID':>10}")
    lines.append("-" * 58)
    for o in sells[:depth][::-1]:
        lines.append(f"{float(o.price):12.2f} {_qty_of(o):10d} {int(o.order_id):10d}")
    if not sells:
        lines.append("(no asks)")
    lines.append("-" * 58)
    # spread line
    best_bid = max((float(o.price) for o in buys), default=None)
    best_ask = min((float(o.price) for o in sells), default=None)
    if best_bid is not None and best_ask is not None:
        spread = best_ask - best_bid
        mid = (best_ask + best_bid) / 2
        lines.append(f"Best Bid: {best_bid:.2f} | Best Ask: {best_ask:.2f} | "
                     f"Spread: {spread:.2f} | Mid: {mid:.2f}")
    else:
        lines.append("Book one-sided (no spread yet)")
    lines.append("-" * 58)
    lines.append(f"{'BIDS (buy)':^58}")
    lines.append(f"{'Price':>12} {'Qty':>10} {'OrderID':>10}")
    lines.append("-" * 58)
    for o in buys[:depth]:
        lines.append(f"{float(o.price):12.2f} {_qty_of(o):10d} {int(o.order_id):10d}")
    if not buys:
        lines.append("(no bids)")
    lines.append("=" * 58)
    return "\n".join(lines)


def render_trades_text(trades: list[dict], limit: int = 15, whale_threshold: int | None = None) -> str:
    lines = ["-" * 58, f"TRADES (showing last {limit})", "-" * 58,
             f"{'BuyID':>8} {'SellID':>8} {'Price':>10} {'Qty':>8}  Flag"]
    for t in trades[-limit:]:
        qty = int(t.get("qty", 0))
        flag = ""
        if whale_threshold is not None and qty >= whale_threshold:
            flag = "<<< WHALE"
        lines.append(f"{int(t.get('buy_id', -1)):8d} {int(t.get('sell_id', -1)):8d} "
                     f"{float(t.get('price', 0.0)):10.2f} {qty:8d}  {flag}")
    if not trades:
        lines.append("(no trades yet)")
    return "\n".join(lines)


def render_latency_text(stats: dict) -> str:
    if stats.get("count", 0) == 0:
        return "LATENCY: no samples collected."
    lines = ["-" * 58, "LATENCY (end-to-end per order)", "-" * 58,
             f"Samples : {stats['count']}",
             f"Mean    : {stats['mean_us']:.3f} us",
             f"Min     : {stats['min_us']:.3f} us",
             f"Max     : {stats['max_us']:.3f} us",
             f"p50     : {stats['p50_us']:.3f} us",
             f"p95     : {stats['p95_us']:.3f} us",
             f"p99     : {stats['p99_us']:.3f} us"]
    if stats.get("throughput_ops"):
        lines.append(f"Throughput: {stats['throughput_ops']:.1f} orders/sec")
    return "\n".join(lines)


def render_dashboard(book, trades: list[dict], latency_stats: dict,
                     whale_threshold: int | None = None) -> str:
    parts = [render_order_book_text(book),
             render_trades_text(trades, whale_threshold=whale_threshold),
             render_latency_text(latency_stats)]
    return "\n\n".join(parts)


# ---------------- matplotlib (optional) ----------------

def _mpl():
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        return plt
    except Exception as exc:  # pragma: no cover
        print(f"[visualizer] matplotlib unavailable, skipping plot ({exc})")
        return None


def plot_latency_histogram(latencies_us: list[float], save_path: str) -> str | None:
    plt = _mpl()
    if plt is None or not latencies_us:
        return None
    os.makedirs(os.path.dirname(os.path.abspath(save_path)), exist_ok=True)
    fig, ax = plt.subplots(figsize=(8, 4.5))
    ax.hist(latencies_us, bins=40, color="#1f77b4", edgecolor="black", alpha=0.8)
    ax.set_title("ChronosMatch per-order latency (microseconds)")
    ax.set_xlabel("Latency (us)")
    ax.set_ylabel("Count")
    fig.tight_layout()
    fig.savefig(save_path, dpi=150)
    plt.close(fig)
    return save_path


def plot_orderbook_depth(book, save_path: str) -> str | None:
    plt = _mpl()
    if plt is None:
        return None
    from .metrics import OrderBookAnalytics
    bids = OrderBookAnalytics.depth_by_price(list(getattr(book, "buy_orders", [])))
    asks = OrderBookAnalytics.depth_by_price(list(getattr(book, "sell_orders", [])))
    if not bids and not asks:
        return None
    os.makedirs(os.path.dirname(os.path.abspath(save_path)), exist_ok=True)
    fig, ax = plt.subplots(figsize=(8, 4.5))
    if bids:
        ax.bar(list(bids.keys()), list(bids.values()), color="green", alpha=0.7, label="Bids")
    if asks:
        ax.bar(list(asks.keys()), list(asks.values()), color="red", alpha=0.7, label="Asks")
    ax.set_title("ChronosMatch order-book depth by price")
    ax.set_xlabel("Price")
    ax.set_ylabel(" resting quantity")
    ax.legend()
    fig.tight_layout()
    fig.savefig(save_path, dpi=150)
    plt.close(fig)
    return save_path


def plot_trade_prices(trades: list[dict], save_path: str) -> str | None:
    plt = _mpl()
    if plt is None or not trades:
        return None
    os.makedirs(os.path.dirname(os.path.abspath(save_path)), exist_ok=True)
    prices = [float(t.get("price", 0.0)) for t in trades]
    fig, ax = plt.subplots(figsize=(8, 4.5))
    ax.plot(range(len(prices)), prices, marker="o", markersize=3, linewidth=1)
    ax.set_title("ChronosMatch execution price trail")
    ax.set_xlabel("Trade index")
    ax.set_ylabel("Price")
    fig.tight_layout()
    fig.savefig(save_path, dpi=150)
    plt.close(fig)
    return save_path

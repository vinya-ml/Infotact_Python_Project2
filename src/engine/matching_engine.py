"""Matching engine module providing Cython engine or pure-Python reference engine.

If the Cython extension (matching_engine.pyx -> .pyd/.so) has been compiled via
`python setup.py build_ext --inplace`, the native C-compiled engine is loaded.
Otherwise, this module provides the pure-Python reference engine implementing the
exact interface specified in SCHEMA.md and matching_engine.pyx.
"""

from __future__ import annotations

import sys
from typing import Any

BUY = 0
SELL = 1

# Flag indicating whether compiled Cython extension is active
HAS_CYTHON_ENGINE = False

try:
    # Try loading compiled native Cython extension if present
    # To avoid importing this .py file recursively, check if C extension exists
    import importlib.machinery
    import importlib.util
    import os

    _cur_dir = os.path.dirname(os.path.abspath(__file__))
    _found_pyd = False
    for fname in os.listdir(_cur_dir):
        if fname.startswith("matching_engine.") and (fname.endswith(".pyd") or fname.endswith(".so")):
            ext_path = os.path.join(_cur_dir, fname)
            spec = importlib.util.spec_from_file_location("src.engine._matching_engine_cython", ext_path)
            if spec and spec.loader:
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                MatchingEngine = getattr(mod, "MatchingEngine")
                COrderObj = getattr(mod, "COrderObj", None)
                BUY = getattr(mod, "BUY", BUY)
                SELL = getattr(mod, "SELL", SELL)
                HAS_CYTHON_ENGINE = True
                _found_pyd = True
                break
    if not _found_pyd:
        raise ImportError("No compiled Cython binary found in engine directory.")
except Exception:
    HAS_CYTHON_ENGINE = False


if not HAS_CYTHON_ENGINE:
    class COrderObj:
        """Python-visible order wrapper matching the Cython COrderObj interface."""
        __slots__ = ("order_id", "side", "price", "quantity", "timestamp")

        def __init__(self, order_id: int, side: int, price: float, quantity: int, timestamp: int):
            self.order_id = int(order_id)
            self.side = int(side)
            self.price = float(price)
            self.quantity = int(quantity)
            self.timestamp = int(timestamp)

        def __repr__(self) -> str:
            return f"COrderObj(order_id={self.order_id}, side={self.side}, price={self.price}, quantity={self.quantity})"

    class MatchingEngine:
        """Reference Price-Time Priority Limit Order Book matching engine.

        Implements the exact schema and interface defined in SCHEMA.md
        and src/engine/matching_engine.pyx.
        """

        def __init__(self) -> None:
            self.order_count: int = 0
            self.buy_orders: list[COrderObj] = []
            self.sell_orders: list[COrderObj] = []
            self.trades: list[dict[str, Any]] = []

        def add_order(
            self,
            order_id: int,
            side: int,
            price: float,
            quantity: int,
            timestamp: int,
        ) -> int:
            """Receive an order and execute Price-Time Priority matching."""
            self.order_count += 1
            wrapped = COrderObj(order_id, side, price, quantity, timestamp)

            if side == BUY:
                self._match_buy(wrapped)
            else:
                self._match_sell(wrapped)

            return int(order_id)

        def _match_buy(self, order: COrderObj) -> None:
            """Match an incoming BUY order against resting SELL orders."""
            self.sell_orders.sort(key=lambda o: (o.price, o.timestamp))

            while order.quantity > 0 and self.sell_orders and self.sell_orders[0].price <= order.price:
                best_sell = self.sell_orders[0]
                matched_qty = min(order.quantity, best_sell.quantity)

                self.trades.append({
                    "buy_id": order.order_id,
                    "sell_id": best_sell.order_id,
                    "price": best_sell.price,
                    "qty": matched_qty,
                })

                order.quantity -= matched_qty
                best_sell.quantity -= matched_qty

                if best_sell.quantity == 0:
                    self.sell_orders.pop(0)

            if order.quantity > 0:
                self.buy_orders.append(order)

        def _match_sell(self, order: COrderObj) -> None:
            """Match an incoming SELL order against resting BUY orders."""
            self.buy_orders.sort(key=lambda o: (-o.price, o.timestamp))

            while order.quantity > 0 and self.buy_orders and self.buy_orders[0].price >= order.price:
                best_buy = self.buy_orders[0]
                matched_qty = min(order.quantity, best_buy.quantity)

                self.trades.append({
                    "buy_id": best_buy.order_id,
                    "sell_id": order.order_id,
                    "price": best_buy.price,
                    "qty": matched_qty,
                })

                order.quantity -= matched_qty
                best_buy.quantity -= matched_qty

                if best_buy.quantity == 0:
                    self.buy_orders.pop(0)

            if order.quantity > 0:
                self.sell_orders.append(order)

        def get_order_count(self) -> int:
            """Return total number of orders received."""
            return self.order_count

        def get_trades(self) -> list[dict[str, Any]]:
            """Return all matched trades so far."""
            return self.trades

        def get_order_book_snapshot(self) -> list[float | None]:
            """Return current top-of-book state: [best_bid, best_ask]."""
            best_bid = self.buy_orders[0].price if self.buy_orders else None
            best_ask = self.sell_orders[0].price if self.sell_orders else None
            return [best_bid, best_ask]

__all__ = ["MatchingEngine", "COrderObj", "BUY", "SELL", "HAS_CYTHON_ENGINE"]

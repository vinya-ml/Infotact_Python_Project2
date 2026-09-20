class Order:
    def __init__(self, order_id, side, price, qty, timestamp):
        self.order_id = order_id
        self.side = side          # 'buy' or 'sell' (lowercase, internal format)
        self.price = price
        self.qty = qty
        self.timestamp = timestamp

    def __repr__(self):
        return f"Order({self.order_id}, {self.side}, {self.price}, {self.qty})"


def adapt_order(processor_order):
    """
    Convert an Order coming from Member 1's data_processing module
    into the format expected by this OrderBook.

    Member 1's Order uses:
        side          -> 'BUY' / 'SELL' (uppercase)
        quantity      -> instead of qty
        timestamp_ns  -> instead of timestamp

    This adapter bridges that difference without changing the
    core matching logic below.
    """
    return Order(
        order_id=processor_order.order_id,
        side=processor_order.side.lower(),       # 'BUY' -> 'buy'
        price=processor_order.price,
        qty=processor_order.quantity,             # quantity -> qty
        timestamp=processor_order.timestamp_ns    # timestamp_ns -> timestamp
    )


class OrderBook:
    def __init__(self):
        self.buy_orders = []   # will keep sorted: highest price first
        self.sell_orders = []  # will keep sorted: lowest price first
        self.trades = []

    def add_order(self, order):
        if order.side == 'buy':
            self._match_buy(order)
        else:
            self._match_sell(order)

    def _match_buy(self, order):
        # Sort sell orders: lowest price first, earliest timestamp first
        self.sell_orders.sort(key=lambda o: (o.price, o.timestamp))

        while order.qty > 0 and self.sell_orders and self.sell_orders[0].price <= order.price:
            best_sell = self.sell_orders[0]
            matched_qty = min(order.qty, best_sell.qty)

            self.trades.append({
                'buy_id': order.order_id,
                'sell_id': best_sell.order_id,
                'price': best_sell.price,
                'qty': matched_qty
            })

            order.qty -= matched_qty
            best_sell.qty -= matched_qty

            if best_sell.qty == 0:
                self.sell_orders.pop(0)

        # If any quantity remains unmatched, rest it in the book
        if order.qty > 0:
            self.buy_orders.append(order)

    def _match_sell(self, order):
        # Sort buy orders: highest price first, earliest timestamp first
        self.buy_orders.sort(key=lambda o: (-o.price, o.timestamp))

        while order.qty > 0 and self.buy_orders and self.buy_orders[0].price >= order.price:
            best_buy = self.buy_orders[0]
            matched_qty = min(order.qty, best_buy.qty)

            self.trades.append({
                'buy_id': best_buy.order_id,
                'sell_id': order.order_id,
                'price': best_buy.price,
                'qty': matched_qty
            })

            order.qty -= matched_qty
            best_buy.qty -= matched_qty

            if best_buy.qty == 0:
                self.buy_orders.pop(0)

        # If any quantity remains unmatched, rest it in the book
        if order.qty > 0:
            self.sell_orders.append(order)
class Order:
    def __init__(self, order_id, side, price, qty, timestamp):
        self.order_id = order_id
        self.side = side          # 'buy' or 'sell'
        self.price = price
        self.qty = qty
        self.timestamp = timestamp

    def __repr__(self):
        return f"Order({self.order_id}, {self.side}, {self.price}, {self.qty})"
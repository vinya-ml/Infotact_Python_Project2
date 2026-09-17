from baseline_matcher import Order, OrderBook


def test_full_match():
    book = OrderBook()
    book.add_order(Order(1, 'buy', 100.5, 50, 1))
    book.add_order(Order(2, 'sell', 100.5, 50, 2))

    assert len(book.trades) == 1
    assert book.trades[0]['qty'] == 50
    print("test_full_match passed")


def test_partial_match():
    book = OrderBook()
    book.add_order(Order(1, 'buy', 101, 100, 1))
    book.add_order(Order(2, 'sell', 100, 40, 2))

    assert len(book.trades) == 1
    assert book.trades[0]['qty'] == 40
    assert book.buy_orders[0].qty == 60  # remaining unmatched qty rests in book
    print("test_partial_match passed")


def test_no_match():
    book = OrderBook()
    book.add_order(Order(1, 'buy', 99, 10, 1))
    book.add_order(Order(2, 'sell', 100, 10, 2))

    assert len(book.trades) == 0
    assert len(book.buy_orders) == 1
    assert len(book.sell_orders) == 1
    print("test_no_match passed")


def test_multiple_partial_fills():
    book = OrderBook()
    book.add_order(Order(1, 'sell', 100, 30, 1))
    book.add_order(Order(2, 'sell', 100, 20, 2))
    book.add_order(Order(3, 'buy', 100, 40, 3))

    assert len(book.trades) == 2
    assert book.trades[0]['qty'] == 30
    assert book.trades[1]['qty'] == 10
    assert book.sell_orders[0].qty == 10  # leftover from second sell order
    print("test_multiple_partial_fills passed")


if __name__ == "__main__":
    test_full_match()
    test_partial_match()
    test_no_match()
    test_multiple_partial_fills()
    print("\nAll tests passed!")
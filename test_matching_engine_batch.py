import pytest

from src.engine.matching_engine import MatchingEngine

BUY = 0
SELL = 1


def make_orders(n=100):
    return [
        (i, BUY if i % 2 else SELL,
         100.0 if i % 2 else 99.0, 10, i)
        for i in range(1, n + 1)
    ]


def test_batch_processes_all_orders():
    engine = MatchingEngine()
    processed = engine.add_orders_batch(make_orders(100))

    assert processed == 100
    assert engine.get_order_count() == 100
    assert engine.get_trade_count() == 50
    assert engine.get_order_book_snapshot() == [None, None]


def test_batch_matches_sequential_processing():
    orders = make_orders(100)
    batch_engine = MatchingEngine()
    single_engine = MatchingEngine()

    batch_engine.add_orders_batch(orders)

    for order in orders:
        single_engine.add_order(*order)

    assert batch_engine.get_order_count() == single_engine.get_order_count()
    assert batch_engine.get_trade_count() == single_engine.get_trade_count()
    assert batch_engine.get_trades() == single_engine.get_trades()
    assert (
        batch_engine.get_order_book_snapshot()
        == single_engine.get_order_book_snapshot()
    )


def test_empty_batch():
    engine = MatchingEngine()

    assert engine.add_orders_batch([]) == 0
    assert engine.get_order_count() == 0
    assert engine.get_trade_count() == 0


def test_batch_rejects_wrong_tuple_length():
    engine = MatchingEngine()

    with pytest.raises(ValueError):
        engine.add_orders_batch([(1, BUY, 100.0)])


def test_batch_rejects_invalid_side():
    engine = MatchingEngine()

    with pytest.raises(ValueError):
        engine.add_orders_batch([(1, 2, 100.0, 10, 1)])


def test_batch_rejects_invalid_price():
    engine = MatchingEngine()

    with pytest.raises(ValueError):
        engine.add_orders_batch([(1, BUY, 0.0, 10, 1)])


def test_batch_rejects_zero_quantity():
    engine = MatchingEngine()

    with pytest.raises(ValueError):
        engine.add_orders_batch([(1, BUY, 100.0, 0, 1)])

"""
Test script for the Cython MatchingEngine (src/engine/matching_engine.pyx).

This will only run successfully AFTER the .pyx file has been compiled
(i.e., after running `python setup.py build_ext --inplace` from the
project root, which requires Microsoft C++ Build Tools to be installed).

Run this from the project root:
    python test_matching_engine.py
"""

from src.engine.matching_engine import MatchingEngine

# Side constants must match the ones defined in matching_engine.pyx
BUY = 0
SELL = 1


def test_full_match():
    engine = MatchingEngine()
    engine.add_order(1, BUY, 100.5, 50, 1)
    engine.add_order(2, SELL, 100.5, 50, 2)

    trades = engine.get_trades()
    assert len(trades) == 1
    assert trades[0]['qty'] == 50
    print("test_full_match passed")


def test_partial_match():
    engine = MatchingEngine()
    engine.add_order(1, BUY, 101, 100, 1)
    engine.add_order(2, SELL, 100, 40, 2)

    trades = engine.get_trades()
    assert len(trades) == 1
    assert trades[0]['qty'] == 40
    print("test_partial_match passed")


def test_no_match():
    engine = MatchingEngine()
    engine.add_order(1, BUY, 99, 10, 1)
    engine.add_order(2, SELL, 100, 10, 2)

    trades = engine.get_trades()
    assert len(trades) == 0
    print("test_no_match passed")


def test_multiple_partial_fills():
    engine = MatchingEngine()
    engine.add_order(1, SELL, 100, 30, 1)
    engine.add_order(2, SELL, 100, 20, 2)
    engine.add_order(3, BUY, 100, 40, 3)

    trades = engine.get_trades()
    assert len(trades) == 2
    assert trades[0]['qty'] == 30
    assert trades[1]['qty'] == 10
    print("test_multiple_partial_fills passed")


def test_order_count():
    engine = MatchingEngine()
    engine.add_order(1, BUY, 100, 10, 1)
    engine.add_order(2, SELL, 100, 10, 2)

    assert engine.get_order_count() == 2
    print("test_order_count passed")


def test_order_book_snapshot():
    engine = MatchingEngine()
    engine.add_order(1, BUY, 99, 10, 1)   # rests, no match
    engine.add_order(2, SELL, 105, 10, 2)  # rests, no match

    best_bid, best_ask = engine.get_order_book_snapshot()
    assert best_bid == 99
    assert best_ask == 105
    print("test_order_book_snapshot passed")


if __name__ == "__main__":
    test_full_match()
    test_partial_match()
    test_no_match()
    test_multiple_partial_fills()
    test_order_count()
    test_order_book_snapshot()
    print("\nAll Cython engine tests passed!")
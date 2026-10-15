"""Engine tests on synthetic data.   ../venv/bin/python test_engine.py   (or pytest)"""
import contextlib

import numpy as np
import pandas as pd

try:
    import pytest
except ImportError:                      # the agent venv has no pytest; minimal stand-in
    class pytest:
        @staticmethod
        def approx(x, rel=1e-9):
            class A:
                def __eq__(self, o):
                    return abs(o - x) <= rel * max(abs(x), 1e-12)
            return A()

        @staticmethod
        @contextlib.contextmanager
        def raises(exc):
            try:
                yield
            except exc:
                return
            raise AssertionError(f"{exc.__name__} not raised")

from engine import SLIPPAGE, LookaheadError, Panel, Signal, run_backtest, simulate_trade
from strategies import cheater, random_liquid


def panel_from(bars):
    """bars: {ticker: [(o,h,l,c), ...]} all same length; volume large enough to be liquid."""
    rows = []
    for t, seq in bars.items():
        for k, (o, h, l, c) in enumerate(seq):
            rows.append(dict(date=f"2025-01-{k + 1:02d}", ticker=t, o=o, h=h, l=l, c=c, v=10_000_000))
    return Panel.from_long(pd.DataFrame(rows))


FLAT = (10, 10, 10, 10)


def test_entry_next_open_and_slippage_time_exit():
    p = panel_from({"A": [FLAT, (11, 11.5, 10.8, 11.2), (11.2, 11.4, 11, 11.3)]})
    t = simulate_trade(p, 0, 0, Signal("A", 2, stop_pct=0.5, target_pct=0.5))
    assert t["entry"] == pytest.approx(11 * (1 + SLIPPAGE))
    assert t["exit"] == pytest.approx(11.3 * (1 - SLIPPAGE)) and t["reason"] == "time"


def test_stop_wins_when_both_hit_same_day():
    p = panel_from({"A": [FLAT, (10, 12, 8, 10)]})
    t = simulate_trade(p, 0, 0, Signal("A", 5, stop_pct=0.1, target_pct=0.1))
    assert t["reason"] == "stop" and t["exit"] == pytest.approx(9 * (1 - SLIPPAGE))


def test_gap_through_stop_fills_at_open():
    p = panel_from({"A": [FLAT, FLAT, (8, 8.5, 7.5, 8)]})
    t = simulate_trade(p, 0, 0, Signal("A", 5, stop_pct=0.05, target_pct=0.5))
    assert t["reason"] == "stop" and t["exit"] == pytest.approx(8 * (1 - SLIPPAGE))


def test_gap_through_target_fills_at_open():
    p = panel_from({"A": [FLAT, FLAT, (12, 12.5, 11.8, 12)]})
    t = simulate_trade(p, 0, 0, Signal("A", 5, stop_pct=0.5, target_pct=0.1))
    assert t["reason"] == "target" and t["exit"] == pytest.approx(12 * (1 - SLIPPAGE))


def test_stop_dist_is_below_entry_open_and_inf_target_never_hits():
    p = panel_from({"A": [FLAT, (11, 30, 10.6, 11), (11, 11, 9.4, 9.6)]})
    t = simulate_trade(p, 0, 0, Signal("A", 5, stop_dist=1.5, target=np.inf))
    assert t["reason"] == "stop" and t["exit_i"] == 2
    assert t["exit"] == pytest.approx(9.5 * (1 - SLIPPAGE))      # 11 open - 1.5


def test_illiquid_and_cheap_stocks_dropped():
    p = panel_from({"A": [(4, 4, 4, 4)] * 70})
    tr = run_backtest(lambda v: [Signal("A", 3, stop_pct=.1, target_pct=.1)], p, n_lookahead_checks=3)
    assert tr.empty and tr.attrs["dropped_illiquid"] > 0


def _random_walk_panel(n_days=250, n_tickers=40, seed=1):
    rng = np.random.default_rng(seed)
    bars = {}
    for k in range(n_tickers):
        c = 50 * np.exp(np.cumsum(rng.normal(0, 0.02, n_days)))
        o = c * np.exp(rng.normal(0, 0.005, n_days))
        h = np.maximum(o, c) * (1 + abs(rng.normal(0, 0.01, n_days)))
        l = np.minimum(o, c) * (1 - abs(rng.normal(0, 0.01, n_days)))
        bars[f"T{k}"] = list(zip(o, h, l, c))
    return panel_from(bars) if n_days <= 31 else _panel_long(bars)


def _panel_long(bars):
    dates = pd.bdate_range("2024-01-01", periods=len(next(iter(bars.values())))).strftime("%Y-%m-%d")
    rows = [dict(date=dates[k], ticker=t, o=o, h=h, l=l, c=c, v=1e7)
            for t, seq in bars.items() for k, (o, h, l, c) in enumerate(seq)]
    return Panel.from_long(pd.DataFrame(rows))


def test_lookahead_guard_catches_cheater():
    with pytest.raises(LookaheadError):
        run_backtest(cheater, _random_walk_panel(), n_lookahead_checks=5)


def test_signal_on_non_trading_ticker_raises():
    p = _random_walk_panel(n_days=100, n_tickers=3)
    p.c[70:, 0] = np.nan
    with pytest.raises(LookaheadError):
        run_backtest(lambda v: [Signal(v.tickers[0], 3, stop_pct=.1, target_pct=.1)], p, n_lookahead_checks=2)


def test_random_strategy_on_random_walk_is_not_profitable():
    tr = run_backtest(random_liquid, _random_walk_panel(n_days=400, n_tickers=60))
    se = tr.ret.std() / np.sqrt(len(tr))
    # Zero-drift walk: expected ~ -2*slippage (minus a bit for stop-first ties).
    assert tr.ret.mean() < 2 * se, (tr.ret.mean(), se)


if __name__ == "__main__":
    fails = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("PASS", name)
            except Exception as e:
                fails += 1
                print("FAIL", name, repr(e))
    raise SystemExit(fails)

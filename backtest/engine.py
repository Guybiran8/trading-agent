"""Daily-bar swing backtest engine.

Strategy contract
-----------------
    def my_strategy(view: DataView) -> list[Signal]

`view` holds data up to and including day X only (view.i is X's row). Return the
stocks to buy. Entry is ALWAYS at the next trading day's open; stop/target are
either absolute prices, percentages of that entry open, or (stop only) a fixed
distance below that entry open (stop_dist). Strategies must be
deterministic given the view (seed any randomness from view.date).

Rules enforced here
-------------------
- Entry at next day's open; exit on stop, target, or close of the last hold day.
- A day whose range touches both stop and target counts as the stop.
- Gaps: a stop gapped through fills at the open (worse), a target gapped through
  fills at the open (better).
- 0.1% slippage per side.
- Signals on stocks under $5 close or under $5M 20-day avg dollar volume (as of
  day X) are dropped.
- Look-ahead guard: on a sample of days the strategy is re-run on a copy of the
  data whose rows AFTER day X are scrambled. Any difference in its output raises
  LookaheadError. Signals on tickers that didn't trade on day X also raise.
"""
import os
import sqlite3
from dataclasses import dataclass

import numpy as np
import pandas as pd

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "daily.sqlite")
SLIPPAGE = 0.001
MIN_PRICE = 5.0
MIN_ADV = 5_000_000
ADV_DAYS = 20


class LookaheadError(Exception):
    pass


@dataclass(frozen=True)
class Signal:
    ticker: str
    max_hold: int                 # trading days, entry day counts as day 1
    stop: float = None            # absolute price, or...
    target: float = None
    stop_pct: float = None        # ...fraction of entry open, e.g. 0.07 = 7% below
    target_pct: float = None      # e.g. 0.10 = 10% above
    stop_dist: float = None       # ...or price distance below entry open, e.g. 2*ATR


# ── Data ──────────────────────────────────────────────────────────────────────

class Panel:
    """Wide [day x ticker] float arrays; NaN where a ticker didn't trade."""

    def __init__(self, dates, tickers, o, h, l, c, v):
        self.dates = np.asarray(dates)
        self.tickers = np.asarray(tickers)
        self.o, self.h, self.l, self.c, self.v = o, h, l, c, v
        self.col = {t: j for j, t in enumerate(self.tickers)}

    @classmethod
    def from_sqlite(cls, path=DB_PATH):
        """Same arrays as from_long, but streams rows straight into numpy:
        pd.read_sql on all bars needs ~2 GB and gets OOM-killed on the VPS."""
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        dates = np.sort(np.array([r[0] for r in con.execute("SELECT DISTINCT date FROM bars")]))
        tickers = np.sort(np.array([r[0] for r in con.execute("SELECT DISTINCT ticker FROM bars")]))
        di = {d: i for i, d in enumerate(dates)}
        ti = {t: j for j, t in enumerate(tickers)}
        arrs = [np.full((len(dates), len(tickers)), np.nan, dtype=np.float64) for _ in "ohlcv"]
        cur = con.execute("SELECT date, ticker, o, h, l, c, v FROM bars")
        while rows := cur.fetchmany(200_000):
            r = np.array([di[x[0]] for x in rows])
            k = np.array([ti[x[1]] for x in rows])
            vals = np.array([x[2:] for x in rows], dtype=np.float64)   # NULL -> NaN
            for f, a in enumerate(arrs):
                a[r, k] = vals[:, f]
        con.close()
        return cls(dates, tickers, *arrs)

    @classmethod
    def from_long(cls, df):
        dates = np.sort(df["date"].unique())
        tickers = np.sort(df["ticker"].unique())
        di = pd.Index(dates).get_indexer(df["date"])
        ti = pd.Index(tickers).get_indexer(df["ticker"])
        arrs = []
        for f in "ohlcv":
            a = np.full((len(dates), len(tickers)), np.nan, dtype=np.float64)
            a[di, ti] = df[f].to_numpy(dtype=np.float64)
            arrs.append(a)
        return cls(dates, tickers, *arrs)

    def scrambled_after(self, i, rng):
        """Copy whose rows after i are garbage — used by the look-ahead guard."""
        def scr(a):
            b = a.copy()
            b[i + 1:] *= rng.uniform(0.3, 3.0, size=b[i + 1:].shape)
            return b
        return Panel(self.dates, self.tickers, *(scr(a) for a in (self.o, self.h, self.l, self.c, self.v)))


class DataView:
    """What a strategy sees on day X: rows 0..i only."""

    def __init__(self, panel: Panel, i: int):
        self.i = i
        self.date = panel.dates[i]
        self.dates = panel.dates[: i + 1]
        self.tickers = panel.tickers
        self.open = panel.o[: i + 1]
        self.high = panel.h[: i + 1]
        self.low = panel.l[: i + 1]
        self.close = panel.c[: i + 1]
        self.volume = panel.v[: i + 1]

    def liquid(self):
        """Bool mask over tickers passing the price / dollar-volume filter on day X."""
        return liquid_mask(self.close, self.volume)


def liquid_mask(close, volume):
    c = close[-ADV_DAYS:]
    dv = np.nan_to_num(c * volume[-ADV_DAYS:])      # days not traded count as $0
    adv = dv.sum(axis=0) / ADV_DAYS
    last = close[-1]
    with np.errstate(invalid="ignore"):
        return (last >= MIN_PRICE) & (adv >= MIN_ADV) & ~np.isnan(last)


# ── Engine ────────────────────────────────────────────────────────────────────

def _signal_key(sigs):
    return sorted((s.ticker, s.max_hold, s.stop, s.target, s.stop_pct, s.target_pct, s.stop_dist) for s in sigs)


def check_lookahead(strategy, panel, days, seed=0):
    rng = np.random.default_rng(seed)
    for i in days:
        real = _signal_key(strategy(DataView(panel, i)))
        fake = _signal_key(strategy(DataView(panel.scrambled_after(i, rng), i)))
        if real != fake:
            raise LookaheadError(
                f"{getattr(strategy, '__name__', strategy)} output on {panel.dates[i]} changed when "
                f"only data AFTER that day was altered -> it is using future data")


def simulate_trade(panel, j, i, sig):
    """Trade signalled on day i for ticker column j. Returns dict or None if unfilled."""
    e = i + 1
    T = len(panel.dates)
    if e >= T or np.isnan(panel.o[e, j]):
        return None
    open_e = panel.o[e, j]
    if sig.stop is not None:
        stop = sig.stop
    elif sig.stop_dist is not None:
        stop = open_e - sig.stop_dist
    else:
        stop = open_e * (1 - sig.stop_pct)
    target = sig.target if sig.target is not None else open_e * (1 + sig.target_pct)
    entry_fill = open_e * (1 + SLIPPAGE)

    last_day = min(e + sig.max_hold - 1, T - 1)
    exit_px, exit_day, reason = None, None, None
    last_close, last_close_day = None, None
    for d in range(e, last_day + 1):
        o, h, l, c = panel.o[d, j], panel.h[d, j], panel.l[d, j], panel.c[d, j]
        if np.isnan(c):
            continue                              # didn't trade (halt / delisted)
        if l <= stop:                             # stop wins ties with target
            exit_px, exit_day, reason = min(o, stop), d, "stop"
            break
        if h >= target:
            exit_px, exit_day, reason = max(o, target), d, "target"
            break
        last_close, last_close_day = c, d
    if exit_px is None:
        if last_close is None:
            return None
        reached_end = last_day < e + sig.max_hold - 1
        exit_px, exit_day = last_close, last_close_day
        reason = "data_end" if reached_end else ("time" if last_close_day == last_day else "delisted")
    exit_fill = exit_px * (1 - SLIPPAGE)
    return dict(ticker=panel.tickers[j], signal_i=i, entry_i=e, exit_i=exit_day,
                entry=entry_fill, exit=exit_fill, ret=exit_fill / entry_fill - 1, reason=reason)


def run_backtest(strategy, panel, warmup=60, n_lookahead_checks=20, seed=0):
    T = len(panel.dates)
    if T < warmup + 2:
        raise ValueError(f"only {T} days of data; need more than warmup={warmup} + 1")
    days = range(warmup, T - 1)                   # last day has no next open
    check_days = np.unique(np.linspace(warmup, T - 2, n_lookahead_checks).astype(int))
    check_lookahead(strategy, panel, check_days, seed)

    trades, dropped = [], 0
    for i in days:
        view = DataView(panel, i)
        sigs = strategy(view)
        if not sigs:
            continue
        liq = view.liquid()
        for s in sigs:
            j = panel.col.get(s.ticker)
            if j is None or np.isnan(panel.c[i, j]):
                raise LookaheadError(f"signal for {s.ticker} on {panel.dates[i]}: it did not trade that day")
            if not liq[j]:
                dropped += 1
                continue
            t = simulate_trade(panel, j, i, s)
            if t:
                trades.append(t)
    df = pd.DataFrame(trades)
    df.attrs["dropped_illiquid"] = dropped
    return df


def portfolio_curve(trades, panel):
    """Daily returns of an equal-weight portfolio of all open positions (0 when flat)."""
    T = len(panel.dates)
    s, n = np.zeros(T), np.zeros(T)
    for t in trades.itertuples():
        j = panel.col[t.ticker]
        prev = t.entry
        for d in range(t.entry_i, t.exit_i + 1):
            px = t.exit if d == t.exit_i else panel.c[d, j]
            if np.isnan(px):
                continue
            s[d] += px / prev - 1
            n[d] += 1
            prev = px
    return np.divide(s, n, out=np.zeros(T), where=n > 0)


def max_drawdown(daily):
    eq = np.cumprod(1 + daily)
    return float((eq / np.maximum.accumulate(eq) - 1).min()) if len(eq) else 0.0

"""Strategies: fn(view: DataView) -> list[Signal]. See engine.py for the contract."""
import zlib

import numpy as np

from engine import Signal


def random_liquid(view, n=5, stop_pct=0.07, target_pct=0.07, max_hold=10):
    """Validation control: n random liquid stocks per day. Should land near the
    market minus costs; if it's profitable, the engine has a bug."""
    rng = np.random.default_rng(zlib.crc32(str(view.date).encode()))
    idx = np.flatnonzero(view.liquid())
    if len(idx) == 0:
        return []
    pick = rng.choice(idx, size=min(n, len(idx)), replace=False)
    return [Signal(view.tickers[j], max_hold, stop_pct=stop_pct, target_pct=target_pct) for j in pick]


def atr_wilder(view, j, n=14):
    """Wilder ATR(n) of ticker column j as of view's last day (rows 0..i only).
    True range uses the prior row's close; if that's missing (didn't trade) TR = H - L.
    Days the ticker didn't trade are skipped. NaN if fewer than n valid TRs."""
    h, l, c = view.high[:, j], view.low[:, j], view.close[:, j]
    pc = np.concatenate(([np.nan], c[:-1]))
    with np.errstate(invalid="ignore"):
        tr = np.fmax(h - l, np.fmax(np.abs(h - pc), np.abs(l - pc)))   # fmax ignores NaN pc
    tr = tr[~np.isnan(tr)]
    if len(tr) < n:
        return np.nan
    atr = tr[:n].mean()
    for x in tr[n:]:
        atr = (atr * (n - 1) + x) / n
    return atr


def _atr_signal(view, j, max_hold, atr_mult):
    """Long, stop = entry open - atr_mult * ATR(14) as of day t, no target. None if no ATR."""
    atr = atr_wilder(view, j)
    if not (np.isfinite(atr) and atr > 0):
        return None
    return Signal(view.tickers[j], max_hold, stop_dist=atr_mult * atr, target=np.inf)


def breakout_v1(view, lookback=20, vol_mult=1.5, spy_sma=50, max_hold=10, atr_mult=2.0):
    """20-day high breakout on 1.5x volume, only while SPY closes above its 50-day SMA.
    Universe = engine liquid_mask ($5 close, $5M 20d avg $vol). Takes every signal.
    Prior 20 days (t-20..t-1) must all have bars for the ticker."""
    if view.i < max(lookback, spy_sma - 1):
        return []
    js = int(np.searchsorted(view.tickers, "SPY"))
    spy = view.close[-spy_sma:, js]
    if np.isnan(spy).any() or not spy[-1] > spy.mean():
        return []
    hh, vv = view.high[-lookback - 1:-1], view.volume[-lookback - 1:-1]
    complete = ~np.isnan(hh).any(axis=0) & ~np.isnan(vv).any(axis=0)
    with np.errstate(invalid="ignore"):
        hit = (view.liquid() & complete
               & (view.close[-1] > np.nanmax(np.where(complete, hh, 0), axis=0))
               & (view.volume[-1] >= vol_mult * np.where(complete, vv, 0).mean(axis=0)))
    sigs = (_atr_signal(view, j, max_hold, atr_mult) for j in np.flatnonzero(hit))
    return [s for s in sigs if s is not None]


def random_matched(view, max_hold=10, atr_mult=2.0):
    """Control for breakout_v1: on each day it has N signals, N random stocks (no
    replacement) from the same full liquid universe, breakout stocks included.
    Same entry / ATR stop / time exit. Stocks with no ATR(14) yet (< 14 bars, stop
    undefined) are ineligible: walk a random permutation, keep the first N with one."""
    n = len(breakout_v1(view, max_hold=max_hold, atr_mult=atr_mult))
    if n == 0:
        return []
    rng = np.random.default_rng(zlib.crc32(str(view.date).encode()))
    sigs = []
    for j in rng.permutation(np.flatnonzero(view.liquid())):
        s = _atr_signal(view, j, max_hold, atr_mult)
        if s is not None:
            sigs.append(s)
            if len(sigs) == n:
                break
    return sigs


def cheater(view):
    """Deliberately peeks at tomorrow's close via the underlying array. The
    look-ahead guard must reject it (used in tests)."""
    full = view.close.base if view.close.base is not None else view.close
    if view.i + 1 >= len(full):
        return []
    tomorrow = full[view.i + 1] / view.close[-1]
    liq = view.liquid()
    tomorrow = np.where(liq, tomorrow, -np.inf)
    j = int(np.nanargmax(tomorrow))
    return [Signal(view.tickers[j], 1, stop_pct=0.5, target_pct=0.5)] if np.isfinite(tomorrow[j]) else []


STRATEGIES = {
    "random_liquid": random_liquid,
    "breakout_v1": breakout_v1,
    "random_matched": random_matched,
}

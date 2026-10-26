"""Strategies: fn(view: DataView) -> list[Signal]. See engine.py for the contract."""
import zlib

import numpy as np
import pandas as pd

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


def _ema_last(a, span):
    return pd.DataFrame(a).ewm(span=span, adjust=False).mean().to_numpy()[-1]


def swing_candidates(view, min_score=75, window=260):
    """Daily-bar approximation of swing_scanner.analyze_swing_stock on day t.
    No sector / news / earnings inputs (they score 0), so it is stricter than live.
    Returns (cols, close, atr, score, old_stop, old_target) arrays, or None.
    ATR = simple 14-day mean true range, as in indicators.py."""
    if view.i < 170:
        return None
    w = slice(-window, None)
    c, h, l, o, v = (x[w] for x in (view.close, view.high, view.low, view.open, view.volume))
    J = np.flatnonzero(view.liquid() & ~np.isnan(c[-100:]).any(axis=0))
    if len(J) == 0:
        return None
    c, h, l, o, v = c[:, J], h[:, J], l[:, J], o[:, J], v[:, J]
    cf = pd.DataFrame(c).ffill().to_numpy()
    last = c[-1]
    e20, e50, e150 = _ema_last(cf, 20), _ema_last(cf, 50), _ema_last(cf, 150)
    rng20 = np.nanmean((h[-20:] - l[-20:]) / c[-20:], axis=0) * 100
    ok = (rng20 >= 3.0) & (last > e20) & (e20 > e50) & (e50 > e150)
    # accumulation, max 35
    acc = 10.0 * np.all(np.diff(l[-10:], axis=0) > 0, axis=0)
    up = c[-15:] > o[-15:]
    n_up, n_dn = up.sum(0), (~up).sum(0)
    with np.errstate(invalid="ignore", divide="ignore"):
        upv = np.where(up, v[-15:], 0).sum(0) / n_up
        dnv = np.where(~up, v[-15:], 0).sum(0) / n_dn
    acc += 10 * ((n_up > 0) & (n_dn > 0) & (upv > dnv))
    roll = pd.DataFrame(cf).rolling(20)
    bw = (4 * roll.std().to_numpy() / roll.mean().to_numpy())[-6:]
    acc += 8 * np.all(bw[1:] < bw[:-1], axis=0)
    lo10 = np.nanmin(l[-10:], 0)
    acc += 7 * ((np.nanmax(h[-10:], 0) - lo10) / lo10 * 100 < 8.0)
    acc = np.minimum(acc, 35)
    # entry quality
    d20 = (last - e20) / e20
    eq = np.where((d20 > 0) & (d20 < 0.03), 25.0, np.where((d20 > 0) & (d20 < 0.06), 10.0, 0.0))
    eq += 5 * (np.nanmean(v[-5:], 0) < 0.8 * np.nanmean(v[-20:], 0))
    res = np.nanmax(h[-20:], 0)
    dres = (res - last) / last * 100
    near = (dres > 0) & (dres <= 5)
    eq += 15 * near
    nxt = np.where(h[-189:] > res * 1.01, h[-189:], np.inf).min(0)
    room = np.where(near, np.where(np.isfinite(nxt), (nxt - res) / res * 100, 30.0), 0.0)
    ok &= room >= 20
    vroll = pd.DataFrame(v).rolling(20).mean().to_numpy()[-5:]
    inst = 15 * np.any(v[-5:] > 2 * vroll, axis=0)
    score = 25 + acc + eq + inst
    ok &= score >= min_score
    pc = np.vstack([np.full((1, len(J)), np.nan), c[:-1]])
    atr = np.nanmean(np.fmax(h - l, np.fmax(np.abs(h - pc), np.abs(l - pc)))[-14:], 0)
    old_stop = last - 1.5 * atr
    old_target = res * (1 + room / 100 * 0.8)
    ok &= ((old_target - last) / last * 100 >= 5.0) & np.isfinite(atr) & (atr > 0)
    k = np.flatnonzero(ok)
    return J[k], last[k], atr[k], score[k], old_stop[k], old_target[k]


# Live exit rules after the 2026-10-26 fix (exit_rules.py): stop >= 1.5 ATR,
# target = k ATR, time exit after N trading days, max 3 swing alerts a day.
# k and N were picked on in-sample days only (see swing_new docstring).
NEW_STOP_ATR = 1.5
NEW_TARGET_ATR = 2.5
NEW_MAX_HOLD = 10
SWING_DAILY_CAP = 3


def swing_old(view):
    """Swing scanner as it ran before the fix: every qualifying setup (the 3/day cap
    never fired), 1.5 ATR stop, room-based target, no time exit (capped at 60 days)."""
    cand = swing_candidates(view)
    if cand is None:
        return []
    cols, _, _, _, stop, target = cand
    return [Signal(view.tickers[j], 60, stop=float(s), target=float(t))
            for j, s, t in zip(cols, stop, target)]


def _new_exits(close, atr, old_stop, target_atr):
    stop = np.minimum(old_stop, close - NEW_STOP_ATR * atr)     # only ever widen
    return stop, close + target_atr * atr


def swing_new(view, target_atr=None, max_hold=None, cap=SWING_DAILY_CAP):
    """Swing scanner after the fix: top `cap` setups by score, new exit rules.
    k (target_atr) and N (max_hold) chosen by an in-sample grid k in {2, 2.5, 3},
    N in {10, 15, 20}: best mean excess vs SPY after 0.4% round-trip costs was
    k=2.5, N=10 (-0.08%/trade; all 9 cells within noise, |t| < 0.7)."""
    target_atr = NEW_TARGET_ATR if target_atr is None else target_atr
    max_hold = NEW_MAX_HOLD if max_hold is None else max_hold
    cand = swing_candidates(view)
    if cand is None:
        return []
    cols, close, atr, score, old_stop, _ = cand
    top = np.argsort(-score, kind="stable")[:cap]
    stop, target = _new_exits(close[top], atr[top], old_stop[top], target_atr)
    return [Signal(view.tickers[j], max_hold, stop=float(s), target=float(t))
            for j, s, t in zip(cols[top], stop, target)]


def swing_new_random(view):
    """Control for swing_new: on each day it has N signals, N random liquid stocks
    with the same exit rules computed from each stock's own close and ATR."""
    n = len(swing_new(view))
    if n == 0:
        return []
    rng = np.random.default_rng(zlib.crc32(str(view.date).encode()) + 7)
    sigs = []
    for j in rng.permutation(np.flatnonzero(view.liquid())):
        h, l, c = view.high[-15:, j], view.low[-15:, j], view.close[-15:, j]
        if np.isnan(c).any() or np.isnan(h).any() or np.isnan(l).any():
            continue
        atr = np.mean(np.fmax(h[1:] - l[1:], np.fmax(np.abs(h[1:] - c[:-1]), np.abs(l[1:] - c[:-1]))))
        if not atr > 0:
            continue
        stop, target = _new_exits(c[-1], atr, np.inf, NEW_TARGET_ATR)
        sigs.append(Signal(view.tickers[j], NEW_MAX_HOLD, stop=float(stop), target=float(target)))
        if len(sigs) == n:
            break
    return sigs


STRATEGIES = {
    "random_liquid": random_liquid,
    "breakout_v1": breakout_v1,
    "random_matched": random_matched,
    "swing_old": swing_old,
    "swing_new": swing_new,
    "swing_new_random": swing_new_random,
}

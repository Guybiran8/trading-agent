"""Run every strategy in strategies.STRATEGIES and print the per-strategy report,
split into in-sample (first 70% of dates) and out-of-sample (last 30%).

    cd /root/trading-agent/backtest && ../venv/bin/python report.py [--is-only] [names...]

--is-only: strategies are silenced on out-of-sample days, so no OOS trade is ever
simulated, and only the IS row is printed.
"""
import functools
import sys

import numpy as np
import pandas as pd

from engine import Panel, max_drawdown, portfolio_curve, run_backtest
from strategies import STRATEGIES

IS_FRACTION = 0.70
BENCH = "SPY"


def _stats(tr, panel, lo, hi):
    """Metrics for trades signalled in day-index range [lo, hi)."""
    t = tr[(tr.signal_i >= lo) & (tr.signal_i < hi)]
    if t.empty:
        return None
    daily = portfolio_curve(t, panel)[lo:hi + 1]
    top5 = t.nlargest(5, "ret").index
    rest = t.drop(top5)
    daily_rest = portfolio_curve(rest, panel)[lo:hi + 1]

    bj = panel.col[BENCH]
    bo, bc = panel.o[:, bj], panel.c[:, bj]
    spy_matched = np.nanmean([bc[x.exit_i] / bo[x.entry_i] - 1 for x in t.itertuples()])
    spy_daily = np.diff(bc[lo:hi + 1]) / bc[lo:hi]
    return dict(
        n=len(t), win=(t.ret > 0).mean() * 100,
        med=t.ret.median() * 100, mean=t.ret.mean() * 100,
        mean_x5=rest.ret.mean() * 100,
        tot=(np.prod(1 + daily) - 1) * 100, tot_x5=(np.prod(1 + daily_rest) - 1) * 100,
        dd=max_drawdown(daily) * 100,
        spy_trade=spy_matched * 100,
        spy_tot=(bc[hi] / bc[lo] - 1) * 100, spy_dd=max_drawdown(spy_daily) * 100,
    )


def _until(strategy, split):
    """Strategy that only signals on days before `split` (IS-only mode)."""
    @functools.wraps(strategy)
    def gated(view):
        return strategy(view) if view.i < split else []
    return gated


def main(names=None, is_only=False):
    panel = Panel.from_sqlite()
    T = len(panel.dates)
    split = int(T * IS_FRACTION)
    print(f"data {panel.dates[0]}..{panel.dates[-1]} ({T} days, {len(panel.tickers)} tickers); "
          f"IS <{panel.dates[split]} <= OOS" + ("   [IS ONLY: OOS not simulated]" if is_only else ""))
    rows = []
    for name in names or STRATEGIES:
        strat = _until(STRATEGIES[name], split) if is_only else STRATEGIES[name]
        tr = run_backtest(strat, panel)
        periods = (("IS", 0, split),) if is_only else (("IS", 0, split), ("OOS", split, T - 1))
        for label, lo, hi in periods:
            s = _stats(tr, panel, lo, hi)
            if s:
                rows.append(dict(strategy=name, period=label, **s))
    df = pd.DataFrame(rows).set_index(["strategy", "period"])
    df.columns = ["trades", "win%", "med%", "mean%", "mean% -top5", "port%", "port% -top5",
                  "maxDD%", "SPY same-window%", "SPY b&h%", "SPY maxDD%"]
    with pd.option_context("display.width", 250, "display.max_columns", 30):
        print(df.round(2).to_string())


if __name__ == "__main__":
    args = sys.argv[1:]
    main([a for a in args if not a.startswith("--")] or None, is_only="--is-only" in args)

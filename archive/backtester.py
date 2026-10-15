import pandas as pd
import numpy as np
import yfinance as yf
from datetime import datetime, timedelta
from config import (
    BT_LOOKBACK_DAYS, BT_WINDOW, BT_MIN_CANDLES,
    BT_INITIAL_CAP, BT_RISK_PCT, BT_MIN_CONFIDENCE,
    BT_MIN_RR, BT_SLIPPAGE, BT_COMMISSION,
    BT_MAX_HOLD, BT_COOLDOWN_DAYS,
    BUY_THRESHOLD, SELL_THRESHOLD,
)
from indicators import add_indicators
from patterns import detect_patterns
from signal_engine import get_signal
from data_fetcher import get_all_symbols


def get_signal_backtest(symbol, df):
    try:
        df = df.copy()
        df = add_indicators(df)
        if df is None or len(df) < 10:
            return None

        patterns = detect_patterns(df)
        result   = get_signal(df, patterns)

        if result["signal"] == "ERROR":
            return None

        score      = result["score"]
        confidence = result["confidence"]
        price      = result["price"]
        atr        = result["atr"]

        if score >= BUY_THRESHOLD:
            direction = "LONG"
            stop      = round(price - (atr * 1.5), 2)
            target    = round(price + (atr * 4.5), 2)
        elif score <= SELL_THRESHOLD:
            direction = "SHORT"
            stop      = round(price + (atr * 1.5), 2)
            target    = round(price - (atr * 4.5), 2)
        else:
            return None

        return {
            "direction":  direction,
            "entry":      price,
            "stop":       stop,
            "target":     target,
            "confidence": confidence,
            "score":      score,
            "phase":      result["phase"],
            "rsi":        result["rsi"],
            "atr":        atr,
        }

    except:
        return None


def run_backtest(symbols=None, silent=False):
    if symbols is None:
        symbols = get_all_symbols()

    end_date   = datetime.today()
    start_date = end_date - timedelta(days=BT_LOOKBACK_DAYS + 60)

    all_trades        = []
    symbol_last_trade = {}
    processed         = 0
    skipped           = 0
    total             = len(symbols)

    print(f"Backtester starting")
    print(f"Symbols: {total} | Lookback: {BT_LOOKBACK_DAYS}d | Window: {BT_WINDOW} candles")
    print(f"Capital: ${BT_INITIAL_CAP:,.0f} | Risk: {BT_RISK_PCT*100:.1f}% | "
          f"Min confidence: {BT_MIN_CONFIDENCE} | Cooldown: {BT_COOLDOWN_DAYS}d")
    print("-" * 60)

    for i, symbol in enumerate(symbols):
        try:
            raw = yf.download(
                symbol,
                start=start_date.strftime("%Y-%m-%d"),
                end=end_date.strftime("%Y-%m-%d"),
                interval="1d",
                progress=False,
                auto_adjust=True,
            )

            if raw is None or raw.empty or len(raw) < BT_MIN_CANDLES:
                skipped += 1
                continue

            if isinstance(raw.columns, pd.MultiIndex):
                raw.columns = raw.columns.get_level_values(0)
            raw = raw.loc[:, ~raw.columns.duplicated()]

            required = ["Open", "High", "Low", "Close", "Volume"]
            if not all(c in raw.columns for c in required):
                skipped += 1
                continue

            raw = raw[required].copy()
            raw.dropna(inplace=True)
            raw.index = pd.to_datetime(raw.index)

            if len(raw) < BT_MIN_CANDLES:
                skipped += 1
                continue

            for idx in range(BT_WINDOW, len(raw) - BT_MAX_HOLD - 1):
                window       = raw.iloc[idx - BT_WINDOW : idx].copy()
                current_date = raw.index[idx]

                last_trade = symbol_last_trade.get(symbol)
                if last_trade is not None:
                    if (current_date - last_trade).days < BT_COOLDOWN_DAYS:
                        continue

                try:
                    signal = get_signal_backtest(symbol, window)
                except:
                    continue

                if signal is None:
                    continue
                if signal["confidence"] < BT_MIN_CONFIDENCE:
                    continue

                direction  = signal["direction"]
                entry_raw  = float(signal["entry"])
                stop_raw   = float(signal["stop"])
                target_raw = float(signal["target"])

                entry = entry_raw * (1 + BT_SLIPPAGE) if direction == "LONG" else entry_raw * (1 - BT_SLIPPAGE)

                risk_pts   = abs(entry - stop_raw)
                reward_pts = abs(target_raw - entry)

                if risk_pts <= 0:
                    continue

                rr = reward_pts / risk_pts
                if rr < BT_MIN_RR:
                    continue

                shares = max(1, int((BT_INITIAL_CAP * BT_RISK_PCT) / risk_pts))

                outcome    = "open"
                exit_price = None

                for future_idx in range(idx + 1, min(idx + 1 + BT_MAX_HOLD, len(raw))):
                    fc = raw.iloc[future_idx]
                    fh = float(fc["High"])
                    fl = float(fc["Low"])

                    if direction == "LONG":
                        if fl <= stop_raw:
                            outcome    = "loss"
                            exit_price = stop_raw
                            break
                        elif fh >= target_raw:
                            outcome    = "win"
                            exit_price = target_raw
                            break
                    else:
                        if fh >= stop_raw:
                            outcome    = "loss"
                            exit_price = stop_raw
                            break
                        elif fl <= target_raw:
                            outcome    = "win"
                            exit_price = target_raw
                            break

                if exit_price is None:
                    exit_price = float(raw.iloc[min(idx + BT_MAX_HOLD, len(raw) - 1)]["Close"])
                    outcome    = "open"

                if direction == "LONG":
                    pnl = (exit_price - entry) * shares
                else:
                    pnl = (entry - exit_price) * shares
                pnl -= (shares * entry) * BT_COMMISSION * 2

                symbol_last_trade[symbol] = current_date

                all_trades.append({
                    "symbol":     symbol,
                    "date":       current_date.strftime("%Y-%m-%d"),
                    "direction":  direction,
                    "entry":      round(entry, 2),
                    "stop":       round(stop_raw, 2),
                    "target":     round(target_raw, 2),
                    "exit":       round(exit_price, 2),
                    "shares":     shares,
                    "rr":         round(rr, 2),
                    "confidence": signal["confidence"],
                    "score":      signal["score"],
                    "outcome":    outcome,
                    "pnl":        round(pnl, 2),
                    "phase":      signal["phase"],
                    "rsi":        signal["rsi"],
                })

            processed += 1
            if not silent and (i + 1) % 25 == 0:
                print(f"  [{i+1}/{total}] processed={processed} skipped={skipped} trades={len(all_trades)}")

        except:
            skipped += 1
            continue

    print(f"\nDone — processed={processed} skipped={skipped} total_signals={len(all_trades)}")
    return pd.DataFrame(all_trades)


def compute_metrics(df):
    if df.empty:
        print("No trades to analyze.")
        return {}

    closed = df[df["outcome"].isin(["win", "loss"])].copy()
    if closed.empty:
        print("No closed trades.")
        return {}

    wins   = closed[closed["outcome"] == "win"]
    losses = closed[closed["outcome"] == "loss"]

    total_trades  = len(closed)
    win_rate      = len(wins) / total_trades
    avg_win       = wins["pnl"].mean()   if len(wins)   > 0 else 0
    avg_loss      = losses["pnl"].mean() if len(losses) > 0 else 0
    total_pnl     = closed["pnl"].sum()
    profit_factor = wins["pnl"].sum() / abs(losses["pnl"].sum()) if losses["pnl"].sum() != 0 else float("inf")
    avg_rr        = closed["rr"].mean()
    total_return  = (total_pnl / BT_INITIAL_CAP) * 100

    closed_sorted = closed.sort_values("date")
    equity        = BT_INITIAL_CAP + closed_sorted["pnl"].cumsum()
    peak          = equity.cummax()
    max_drawdown  = ((equity - peak) / peak).min() * 100

    daily_pnl = closed_sorted.groupby("date")["pnl"].sum()
    sharpe    = (daily_pnl.mean() / daily_pnl.std() * np.sqrt(252)) if daily_pnl.std() > 0 else 0

    return {
        "total_trades":     total_trades,
        "wins":             len(wins),
        "losses":           len(losses),
        "open":             len(df[df["outcome"] == "open"]),
        "win_rate":         win_rate,
        "avg_win":          avg_win,
        "avg_loss":         avg_loss,
        "profit_factor":    profit_factor,
        "avg_rr":           avg_rr,
        "total_pnl":        total_pnl,
        "total_return_pct": total_return,
        "max_drawdown_pct": max_drawdown,
        "sharpe":           sharpe,
    }


def display_backtest_results(df, metrics):
    if not metrics:
        return

    print()
    print("=" * 60)
    print("  BACKTEST RESULTS")
    print("=" * 60)
    print(f"  Total Trades   : {metrics['total_trades']}  ({metrics['open']} force-closed)")
    print(f"  Wins / Losses  : {metrics['wins']} / {metrics['losses']}")
    print(f"  Win Rate       : {metrics['win_rate']*100:.1f}%")
    print(f"  Avg Win        : ${metrics['avg_win']:,.2f}")
    print(f"  Avg Loss       : ${metrics['avg_loss']:,.2f}")
    print(f"  Profit Factor  : {metrics['profit_factor']:.2f}")
    print(f"  Avg R/R        : {metrics['avg_rr']:.2f}")
    print(f"  Total PnL      : ${metrics['total_pnl']:,.2f}")
    print(f"  Total Return   : {metrics['total_return_pct']:.1f}%")
    print(f"  Max Drawdown   : {metrics['max_drawdown_pct']:.1f}%")
    print(f"  Sharpe Ratio   : {metrics['sharpe']:.2f}")
    print("=" * 60)

    closed = df[df["outcome"].isin(["win", "loss"])]
    if closed.empty:
        return

    cols = ["symbol", "date", "direction", "entry", "exit", "rr", "confidence", "outcome", "pnl"]

    print("\nTop 10 Trades by PnL:")
    print(closed.nlargest(10, "pnl")[cols].to_string(index=False))

    print("\nWorst 10 Trades by PnL:")
    print(closed.nsmallest(10, "pnl")[cols].to_string(index=False))

    print("\nBy Direction:")
    grp = closed.groupby("direction").agg(
        trades   =("pnl", "count"),
        win_rate =("outcome", lambda x: round((x == "win").mean(), 2)),
        total_pnl=("pnl", "sum"),
        avg_rr   =("rr", "mean"),
    ).round(2)
    print(grp.to_string())

    print("\nTop 10 Symbols by PnL:")
    print(closed.groupby("symbol")["pnl"].sum().nlargest(10).to_string())

    print("\nBy Market Phase:")
    phase = closed.groupby("phase").agg(
        trades   =("pnl", "count"),
        win_rate =("outcome", lambda x: round((x == "win").mean(), 2)),
        total_pnl=("pnl", "sum"),
    ).round(2)
    print(phase.to_string())

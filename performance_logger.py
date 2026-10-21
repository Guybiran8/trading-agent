"""Performance logging — tracks all signals and closed trade outcomes."""
import csv
import os
from datetime import date

import yfinance as yf

from config import TRADES_CSV

FIELDNAMES = [
    "symbol", "date", "signal_type", "trade_type", "scanner",
    "entry_price", "stop", "target", "conviction",
    "status", "exit_price", "exit_date", "pnl_pct",
    "target_source",   # "original" | "reconstructed" (target rebuilt after the fact; reported separately)
]


def _ensure_csv():
    if not os.path.exists(TRADES_CSV):
        with open(TRADES_CSV, "w", newline="") as f:
            csv.DictWriter(f, fieldnames=FIELDNAMES).writeheader()


def _get_current_price(symbol: str) -> float:
    try:
        df = yf.Ticker(symbol).history(period="5d", interval="1d")
        if df is None or df.empty:
            return 0
        return float(df["Close"].dropna().iloc[-1])
    except Exception:
        return 0


def check_open_trades() -> None:
    _ensure_csv()
    with open(TRADES_CSV, "r", newline="") as f:
        rows = list(csv.DictReader(f))

    updated = False
    today   = date.today().isoformat()
    prices: dict = {}
    for row in rows:
        if row.get("status") != "OPEN":
            continue
        try:
            entry  = float(row["entry_price"])
            stop   = float(row["stop"])
            target = float(row["target"])
        except (ValueError, TypeError):
            continue

        sym = row["symbol"]
        if sym not in prices:
            prices[sym] = _get_current_price(sym)
        price = prices[sym]
        if not price:
            continue

        signal   = row.get("signal_type", "")
        is_short = signal in ("SHORT", "STRONG SELL", "SELL")

        if is_short:
            hit_stop   = price >= stop
            hit_target = price <= target
        else:
            hit_stop   = price <= stop
            hit_target = price >= target

        if hit_stop:
            row["status"]     = "STOPPED"
            row["exit_price"] = price
            row["exit_date"]  = today
            row["pnl_pct"]    = round((price - entry) / entry * 100 * (-1 if is_short else 1), 2)
            updated = True
        elif hit_target:
            row["status"]     = "TARGET_HIT"
            row["exit_price"] = price
            row["exit_date"]  = today
            row["pnl_pct"]    = round((price - entry) / entry * 100 * (-1 if is_short else 1), 2)
            updated = True

    if updated:
        with open(TRADES_CSV, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=FIELDNAMES)
            w.writeheader()
            w.writerows(rows)


def _pnl(row: dict):
    try:
        return float(row["pnl_pct"])
    except (KeyError, TypeError, ValueError):
        return None


def _group_stats(closed: list) -> dict:
    """A win is a closed trade with P&L > 0, whatever its exit status."""
    pnls = [p for p in (_pnl(r) for r in closed) if p is not None]
    wins = [p for p in pnls if p > 0]
    return {
        "closed":   len(closed),
        "wins":     len(wins),
        "win_rate": len(wins) / len(pnls) * 100 if pnls else 0.0,
        "avg_pnl":  sum(pnls) / len(pnls) if pnls else 0.0,
    }


def summarize(rows: list, active: dict) -> dict:
    """Stats for trades.csv rows plus the live trades being followed.

    Trades whose target was reconstructed after the fact are kept out of the clean numbers.
    """
    closed = [r for r in rows if r.get("status") and r.get("status") != "OPEN"]
    recon  = [r for r in closed if r.get("target_source") == "reconstructed"]
    clean  = [r for r in closed if r.get("target_source") != "reconstructed"]
    live_recon = [s for s, t in active.items() if t.get("target_reconstructed")]
    return {
        "total":      len(rows),
        "open":       len([r for r in rows if r.get("status") == "OPEN"]),
        "clean":      _group_stats(clean),
        "recon":      _group_stats(recon),
        "live_clean": len(active) - len(live_recon),
        "live_recon": sorted(live_recon),
    }


def print_performance_summary() -> None:
    from active_trades import load_trades
    _ensure_csv()
    with open(TRADES_CSV, "r", newline="") as f:
        rows = list(csv.DictReader(f))
    s = summarize(rows, load_trades())
    c, rc = s["clean"], s["recon"]
    print(f"\n=== Trade Performance ===")
    print(f"Total: {s['total']} | Closed: {c['closed']} | Wins: {c['wins']} | Open: {s['open']}")
    if c["closed"]:
        print(f"Win rate: {c['win_rate']:.1f}% | Avg P&L: {c['avg_pnl']:.2f}%")
    print(f"Live (followed): {s['live_clean']}")
    if s["live_recon"] or rc["closed"]:
        print(f"--- Reconstructed-target trades (excluded above) ---")
        print(f"Live: {len(s['live_recon'])} {s['live_recon']} | Closed: {rc['closed']} | Wins: {rc['wins']}")
        if rc["closed"]:
            print(f"Win rate: {rc['win_rate']:.1f}% | Avg P&L: {rc['avg_pnl']:.2f}%")

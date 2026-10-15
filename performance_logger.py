"""Performance logging — tracks all signals and closed trade outcomes."""
import csv
import os
import requests
from datetime import date
from config import POLYGON_API_KEY, TRADES_CSV

FIELDNAMES = [
    "symbol", "date", "signal_type", "trade_type", "scanner",
    "entry_price", "stop", "target", "conviction",
    "status", "exit_price", "exit_date", "pnl_pct",
]


def _ensure_csv():
    if not os.path.exists(TRADES_CSV):
        with open(TRADES_CSV, "w", newline="") as f:
            csv.DictWriter(f, fieldnames=FIELDNAMES).writeheader()


def _get_current_price(symbol: str) -> float:
    try:
        url = (
            f"https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/"
            f"tickers/{symbol}?apiKey={POLYGON_API_KEY}"
        )
        r = requests.get(url, timeout=5).json()
        t = r.get("ticker", {})
        return t.get("day", {}).get("c", 0) or t.get("lastTrade", {}).get("p", 0)
    except Exception:
        return 0


def check_open_trades() -> None:
    _ensure_csv()
    with open(TRADES_CSV, "r", newline="") as f:
        rows = list(csv.DictReader(f))

    updated = False
    today   = date.today().isoformat()
    for row in rows:
        if row.get("status") != "OPEN":
            continue
        try:
            entry  = float(row["entry_price"])
            stop   = float(row["stop"])
            target = float(row["target"])
        except (ValueError, TypeError):
            continue

        price = _get_current_price(row["symbol"])
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


def print_performance_summary() -> None:
    _ensure_csv()
    with open(TRADES_CSV, "r", newline="") as f:
        rows = list(csv.DictReader(f))
    closed = [r for r in rows if r.get("status") in ("STOPPED", "TARGET_HIT")]
    wins   = [r for r in closed if r.get("status") == "TARGET_HIT"]
    open_  = [r for r in rows if r.get("status") == "OPEN"]
    print(f"\n=== Trade Performance ===")
    print(f"Total: {len(rows)} | Closed: {len(closed)} | Wins: {len(wins)} | Open: {len(open_)}")
    if closed:
        win_rate = len(wins) / len(closed) * 100
        pnls = []
        for r in closed:
            try:
                pnls.append(float(r["pnl_pct"]))
            except Exception:
                pass
        avg_pnl = sum(pnls) / len(pnls) if pnls else 0
        print(f"Win rate: {win_rate:.1f}% | Avg P&L: {avg_pnl:.2f}%")

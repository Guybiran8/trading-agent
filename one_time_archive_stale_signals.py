"""One-time script: clean up trades.csv / active_trades.json after the exit-logic fix (2026-10-21).

1. Moves the stale OPEN rows (raw scanner signals from 2026-05-07..05-11, written by an old
   logger and never closed because the Polygon snapshot endpoint is blocked on the free plan)
   out of trades.csv into trades_archive_stale_signals_2026-05.csv, unchanged. Closed rows stay.
   trades.csv gets the new 14-column header (target_source); existing rows get an empty value.
2. Removes HIW from active_trades.json: it was stopped out on 2026-06-01 (trades.csv row and
   account.json both record the close) but was still being watched.
3. The remaining live trades lost their entry targets (price_target was raised on every new
   high), so their current price_target is frozen as original_target with
   target_reconstructed=True. Stats report them separately.

Backups: .fix_backups_20261021/. Safe to re-run: does nothing once applied.
Run once, with the service stopped: ./venv/bin/python3 one_time_archive_stale_signals.py
"""
import csv
import json
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
from performance_logger import FIELDNAMES  # noqa: E402

STALE_DATES  = {"2026-05-07", "2026-05-08", "2026-05-11"}
STALE_SYMBOL = "HIW"


def archive_stale_rows(trades_csv: str, archive_csv: str) -> int:
    with open(trades_csv, newline="") as f:
        reader = csv.DictReader(f)
        header = reader.fieldnames
        rows   = list(reader)

    def is_stale(r):
        return r.get("status") == "OPEN" and r.get("date") in STALE_DATES

    stale = [r for r in rows if is_stale(r)]
    keep  = [r for r in rows if not is_stale(r)]
    assert len(stale) + len(keep) == len(rows)

    if stale:
        exists = os.path.exists(archive_csv)
        with open(archive_csv, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=header)
            if not exists:
                w.writeheader()
            w.writerows(stale)

    if stale or header != FIELDNAMES:
        for r in keep:
            r.setdefault("target_source", "")
        with open(trades_csv, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=FIELDNAMES)
            w.writeheader()
            w.writerows(keep)
    return len(stale)


def fix_active_trades(active_json: str) -> dict:
    with open(active_json) as f:
        trades = json.load(f)
    removed = trades.pop(STALE_SYMBOL, None) is not None
    frozen = []
    for sym, t in trades.items():
        if not t.get("original_target"):
            t["original_target"]      = t.get("price_target", 0)
            t["target_reconstructed"] = True
            frozen.append(sym)
    if removed or frozen:
        tmp = active_json + ".tmp"
        with open(tmp, "w") as f:
            json.dump(trades, f, indent=2, default=str)
        os.replace(tmp, active_json)
    return {"removed": removed, "frozen": frozen}


def main():
    n = archive_stale_rows(f"{BASE}/trades.csv", f"{BASE}/trades_archive_stale_signals_2026-05.csv")
    print(f"Archived {n} stale OPEN rows")
    res = fix_active_trades(f"{BASE}/active_trades.json")
    print(f"Removed {STALE_SYMBOL} from active trades: {res['removed']}")
    print(f"Froze reconstructed targets: {res['frozen']}")


if __name__ == "__main__":
    main()

"""One-time (long-running) backfill: recompute path-based outcomes for every existing
tracking.csv row using Polygon 1-min bars, instead of the old point-in-time snapshots.

This is rate-limited to Polygon's 5 req/min plan (1 API call per row), so backfilling
~3,700 rows takes roughly 12+ hours. It is resumable and saves progress every 20 rows,
so it is safe to stop (Ctrl+C / kill) and rerun — already-backfilled rows are skipped.

tracking.csv.bak (made before the short-sign fix) and tracking.csv.bak2 (if present)
are the pre-existing backups; this script does not overwrite either.

Run: nohup ./venv/bin/python3 one_time_backfill_path_outcomes.py > backfill.log 2>&1 &
"""
import csv
import sys
import time
from datetime import datetime, date, timedelta

from config import TRACKING_CSV, TIMEZONE
from data_fetcher import get_polygon_bars
from passive_tracker import (
    FIELDNAMES, _is_short, _outcome, _checkpoint_deadline, _price_at_or_before,
    path_analysis, _data_suspect_check, _cross_process_lock,
)

UPDATED_COLUMNS = [
    "price_1h", "price_eod", "price_1d", "price_3d",
    "pnl_1h_pct", "pnl_eod_pct", "pnl_1d_pct", "pnl_3d_pct",
    "outcome_1h", "outcome_eod", "outcome_1d", "outcome_3d",
    "hit_stop", "hit_target", "first_hit", "max_favorable_pct", "max_adverse_pct",
    "data_suspect",
]


def _row_key(row: dict):
    return (row.get("symbol"), row.get("alert_date"), row.get("alert_time"))

SAVE_EVERY = 20


def _row_is_fully_backfilled(row: dict) -> bool:
    # A row is "done" once every checkpoint that should be fillable (all of them, for
    # historical rows) has a price AND we've recorded path metrics for it.
    if row.get("first_hit", "") == "" and row.get("hit_stop", "") == "":
        return False
    for h in ("1h", "eod", "1d", "3d"):
        if not row.get(f"price_{h}"):
            # Blank is only "done" if the row is too young for that checkpoint to
            # exist yet — for historical backfill rows that's never true, so a blank
            # price here means we haven't successfully filled it.
            return False
    return True


def main():
    with open(TRACKING_CSV, newline="") as f:
        rows = list(csv.DictReader(f))

    total = len(rows)
    todo = [i for i, r in enumerate(rows) if r.get("alert_price") and not _row_is_fully_backfilled(r)]
    print(f"[{datetime.now().isoformat()}] {total} rows total, {len(todo)} need backfill "
          f"(~{len(todo)/5:.0f} min at 5 req/min)", flush=True)

    done_this_run = 0
    skipped_no_bars = 0

    def _process(row: dict) -> str:
        """Returns 'backfilled', 'no_bars', or 'error' — never raises."""
        try:
            symbol = row["symbol"]
            alert_date = date.fromisoformat(row["alert_date"])
            alert_time_str = row.get("alert_time", "09:30:00") or "09:30:00"
            alert_dt = TIMEZONE.localize(datetime.combine(alert_date, datetime.strptime(alert_time_str, "%H:%M:%S").time()))
            alert_price = float(row.get("alert_price", 0) or 0)
            stop_loss = float(row.get("stop_loss", 0) or 0)
            price_target = float(row.get("price_target", 0) or 0)
            is_short = _is_short(row.get("signal_type", ""))
            stop_pct = abs(alert_price - stop_loss) / alert_price * 100 if alert_price and stop_loss else 3.0

            day3_deadline = _checkpoint_deadline(alert_dt, alert_date, "3d")
            now = datetime.now(TIMEZONE)
            fetch_to = min(now, day3_deadline + timedelta(hours=1))

            bars = get_polygon_bars(symbol, alert_dt, fetch_to)
            if not bars:
                row["data_suspect"] = _data_suspect_check(row, entry_bars=None)
                return "no_bars"

            path = path_analysis(bars, alert_price, stop_loss, price_target, is_short)
            row["hit_stop"] = "yes" if path["hit_stop"] else "no"
            row["hit_target"] = "yes" if path["hit_target"] else "no"
            row["first_hit"] = path["first_hit"]
            row["max_favorable_pct"] = path["max_favorable_pct"]
            row["max_adverse_pct"] = path["max_adverse_pct"]

            for h in ("1h", "eod", "1d", "3d"):
                deadline = _checkpoint_deadline(alert_dt, alert_date, h)
                if now < deadline:
                    continue  # too recent, leave blank — live pipeline will fill it

                price = pnl = outcome = None
                if path["first_hit"] and path["first_hit_time"] and path["first_hit_time"] <= deadline:
                    if path["first_hit"] == "stop":
                        price, outcome = stop_loss, "STOP"
                    else:
                        price, outcome = price_target, "WIN"
                    if price and alert_price:
                        pnl = round((price - alert_price) / alert_price * 100 * (-1 if is_short else 1), 2)
                else:
                    bar_price = _price_at_or_before(bars, deadline)
                    if bar_price and alert_price:
                        price = bar_price
                        pnl = round((price - alert_price) / alert_price * 100 * (-1 if is_short else 1), 2)
                        outcome = _outcome(pnl, stop_pct)

                if price is not None:
                    row[f"price_{h}"] = price
                    row[f"pnl_{h}_pct"] = pnl
                    row[f"outcome_{h}"] = outcome

            row["data_suspect"] = _data_suspect_check(row, entry_bars=bars)
            return "backfilled"
        except Exception as e:
            print(f"[{datetime.now().isoformat()}] row ({row.get('symbol')}) failed: {e}", flush=True)
            return "error"

    dirty_keys = set()  # rows this run has actually changed, since the last save

    for n, i in enumerate(todo, start=1):
        status = _process(rows[i])
        if status == "backfilled":
            done_this_run += 1
            dirty_keys.add(_row_key(rows[i]))
        elif status == "no_bars":
            skipped_no_bars += 1
            dirty_keys.add(_row_key(rows[i]))  # data_suspect may still have been set

        if n % SAVE_EVERY == 0 or n == len(todo):
            # The live service (a separate process) may have appended new alerts or
            # updated other rows since our in-memory `rows` snapshot was taken at
            # startup — up to hours ago. Re-read the current file fresh and merge
            # only the specific columns THIS run computed, under a cross-process
            # lock, instead of blindly overwriting the whole file with our stale copy.
            with _cross_process_lock():
                with open(TRACKING_CSV, newline="") as f:
                    fresh_rows = list(csv.DictReader(f))
                fresh_by_key = {_row_key(r): r for r in fresh_rows}

                for key in dirty_keys:
                    src = next((r for r in rows if _row_key(r) == key), None)
                    target = fresh_by_key.get(key)
                    if src is None or target is None:
                        continue
                    for col in UPDATED_COLUMNS:
                        target[col] = src.get(col, target.get(col, ""))

                with open(TRACKING_CSV, "w", newline="") as f:
                    w = csv.DictWriter(f, fieldnames=FIELDNAMES)
                    w.writeheader()
                    w.writerows(fresh_rows)

            dirty_keys.clear()
            print(f"[{datetime.now().isoformat()}] progress: {n}/{len(todo)} rows "
                  f"(backfilled {done_this_run}, no-bars {skipped_no_bars}) — saved", flush=True)

    print(f"[{datetime.now().isoformat()}] DONE. backfilled={done_this_run} no_bars={skipped_no_bars} "
          f"of {len(todo)} attempted ({total} total rows in file)", flush=True)


if __name__ == "__main__":
    main()

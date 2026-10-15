"""One-time script: correct the SHORT/STRONG SELL sign bug in existing tracking.csv rows.

passive_tracker.py historically computed pnl_x_pct as (price-alert_price)/alert_price
for ALL signal types, including SHORT/STRONG SELL, where a price INCREASE is actually
a loss for the position. This flips the sign for those rows only, and re-derives the
outcome_x labels from the corrected pnl using the same thresholds as passive_tracker._outcome().

tracking.csv.bak (made before this script ran) is the pre-fix backup.
Run once: ./venv/bin/python3 one_time_fix_short_sign.py
"""
import csv
import shutil

TRACKING_CSV = "/root/trading-agent/tracking.csv"
FIELDNAMES = [
    "symbol", "alert_date", "alert_time", "alert_price",
    "signal_type", "scanner", "conviction", "regime",
    "stop_loss", "price_target",
    "price_1h", "price_eod", "price_1d", "price_3d",
    "pnl_1h_pct", "pnl_eod_pct", "pnl_1d_pct", "pnl_3d_pct",
    "outcome_1h", "outcome_eod", "outcome_1d", "outcome_3d",
    "was_confirmed",
]
HORIZONS = ["1h", "eod", "1d", "3d"]
SHORT_TYPES = ("SHORT", "STRONG SELL", "SELL")


def _outcome(pnl_pct: float, stop_pct: float) -> str:
    if pnl_pct >= 5.0:
        return "WIN"
    if pnl_pct <= -stop_pct:
        return "STOP"
    if abs(pnl_pct) < 2.0:
        return "FLAT"
    return "LOSS" if pnl_pct < 0 else "GAIN"


def main():
    with open(TRACKING_CSV, newline="") as f:
        rows = list(csv.DictReader(f))

    fixed_rows = 0
    fixed_cells = 0

    for row in rows:
        if row.get("signal_type") not in SHORT_TYPES:
            continue

        alert_price = row.get("alert_price", "")
        stop_loss = row.get("stop_loss", "")
        try:
            alert_price_f = float(alert_price)
        except (ValueError, TypeError):
            alert_price_f = 0.0
        try:
            stop_pct = abs(alert_price_f - float(stop_loss)) / alert_price_f * 100 if alert_price_f and stop_loss else 3.0
        except (ValueError, TypeError):
            stop_pct = 3.0

        row_changed = False
        for h in HORIZONS:
            pnl_col = f"pnl_{h}_pct"
            outcome_col = f"outcome_{h}"
            raw = row.get(pnl_col, "")
            if raw == "" or raw is None:
                continue
            try:
                pnl = float(raw)
            except (ValueError, TypeError):
                continue

            corrected = round(-pnl, 2)
            if corrected != pnl:
                row[pnl_col] = corrected
                new_outcome = _outcome(corrected, stop_pct)
                if row.get(outcome_col) != new_outcome:
                    row[outcome_col] = new_outcome
                fixed_cells += 1
                row_changed = True

        if row_changed:
            fixed_rows += 1

    with open(TRACKING_CSV, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDNAMES)
        w.writeheader()
        w.writerows(rows)

    print(f"Rows examined (SHORT/STRONG SELL): {sum(1 for r in rows if r.get('signal_type') in SHORT_TYPES)}")
    print(f"Rows with at least one corrected checkpoint: {fixed_rows}")
    print(f"Total pnl/outcome cells corrected: {fixed_cells}")


if __name__ == "__main__":
    main()

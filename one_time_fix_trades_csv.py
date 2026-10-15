"""One-time script: repair the header/data column mismatch in trades.csv.

Root cause: the file's header (written long ago) has 11 columns
(symbol,date,signal_type,trade_type,entry_price,stop,target,status,exit_price,exit_date,pnl_pct).
trade_follower.py's writer was later upgraded to a 13-column schema
(symbol,date,signal_type,trade_type,scanner,entry_price,stop,target,conviction,status,exit_price,exit_date,pnl_pct)
but never rewrote the header (the file already existed, so w.writeheader() was skipped).
Every row appended since then has 13 real values sitting under an 11-name header,
so csv.DictReader silently misaligns them (values overflow into a restkey list).

This script reads the file positionally (not via the stale header), maps each row by
its actual field count, and rewrites the whole file under the correct 13-column header.

trades.csv.bak (made before this script ran) is the pre-fix backup.
Run once: ./venv/bin/python3 one_time_fix_trades_csv.py
"""
import csv

TRADES_CSV = "/root/trading-agent/trades.csv"

OLD_FIELDS = ["symbol", "date", "signal_type", "trade_type",
              "entry_price", "stop", "target",
              "status", "exit_price", "exit_date", "pnl_pct"]

NEW_FIELDS = ["symbol", "date", "signal_type", "trade_type", "scanner",
              "entry_price", "stop", "target", "conviction",
              "status", "exit_price", "exit_date", "pnl_pct"]


def main():
    with open(TRADES_CSV, newline="") as f:
        raw_rows = list(csv.reader(f))

    header, data_rows = raw_rows[0], raw_rows[1:]

    fixed = []
    unrecoverable = []
    for i, r in enumerate(data_rows, start=2):  # line 2 = first data row
        if len(r) == len(OLD_FIELDS):
            d = dict(zip(OLD_FIELDS, r))
            row = {
                "symbol": d["symbol"], "date": d["date"],
                "signal_type": d["signal_type"], "trade_type": d["trade_type"],
                "scanner": "", "entry_price": d["entry_price"],
                "stop": d["stop"], "target": d["target"], "conviction": "",
                "status": d["status"], "exit_price": d["exit_price"],
                "exit_date": d["exit_date"], "pnl_pct": d["pnl_pct"],
            }
            fixed.append(row)
        elif len(r) == len(NEW_FIELDS):
            row = dict(zip(NEW_FIELDS, r))
            fixed.append(row)
        else:
            unrecoverable.append((i, r))

    with open(TRADES_CSV, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=NEW_FIELDS)
        w.writeheader()
        w.writerows(fixed)

    print(f"Total data rows: {len(data_rows)}")
    print(f"Repaired (11-col legacy, scanner/conviction unknown -> blank): "
          f"{sum(1 for r in data_rows if len(r) == len(OLD_FIELDS))}")
    print(f"Repaired (13-col, already correctly ordered, header only was stale): "
          f"{sum(1 for r in data_rows if len(r) == len(NEW_FIELDS))}")
    print(f"Unrecoverable (unexpected column count): {len(unrecoverable)}")
    for line_no, r in unrecoverable:
        print(f"  line {line_no}: {len(r)} fields -> {r}")


if __name__ == "__main__":
    main()

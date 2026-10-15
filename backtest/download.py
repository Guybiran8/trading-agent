"""Download ~2 years of US-stock daily bars via Polygon's grouped-daily endpoint
(one request per trading day) into backtest/data/daily.sqlite.

Grouped daily returns every ticker that traded that day, so delisted names are
included -> no survivorship bias from today's symbol list.

Resumable: each date is committed in its own transaction and recorded in the
`days` table (including market holidays with 0 rows), so a restart skips them.
Free tier = 5 req/min -> one request every 12.5s.
"""
import os
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta

import requests
from dotenv import load_dotenv

HERE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(HERE, "..", ".env"))
API_KEY = os.environ["POLYGON_API_KEY"]

DB_PATH = os.path.join(HERE, "data", "daily.sqlite")
START = date(2024, 10, 1)          # free tier: 2 years of history
MIN_INTERVAL = 12.5                # seconds between requests (5/min)
URL = "https://api.polygon.io/v2/aggs/grouped/locale/us/market/stocks/{d}?adjusted=true&apiKey={k}"


def log(msg):
    print(f"[{datetime.now().isoformat(timespec='seconds')}] {msg}", flush=True)


def init_db(con):
    con.executescript("""
        CREATE TABLE IF NOT EXISTS bars (
            date TEXT NOT NULL, ticker TEXT NOT NULL,
            o REAL, h REAL, l REAL, c REAL, v REAL, vw REAL, n INTEGER,
            PRIMARY KEY (date, ticker));
        CREATE TABLE IF NOT EXISTS days (date TEXT PRIMARY KEY, n_rows INTEGER, fetched_at TEXT);
    """)


def weekdays(start, end):
    d = start
    while d <= end:
        if d.weekday() < 5:
            yield d
        d += timedelta(days=1)


def fetch(d):
    """Return list of results, or raise after repeated failures."""
    backoff = 30
    for attempt in range(8):
        try:
            r = requests.get(URL.format(d=d.isoformat(), k=API_KEY), timeout=60)
            if r.status_code == 200:
                return r.json().get("results") or []
            log(f"{d} HTTP {r.status_code}: {r.text[:120]} -- retry in {backoff}s")
        except Exception as e:
            log(f"{d} error {e} -- retry in {backoff}s")
        time.sleep(backoff)
        backoff = min(backoff * 2, 300)
    raise RuntimeError(f"giving up on {d}")


def main():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    init_db(con)
    done = {r[0] for r in con.execute("SELECT date FROM days")}
    end = date.today() - timedelta(days=1)   # today's bar isn't final yet
    todo = [d for d in weekdays(START, end) if d.isoformat() not in done]
    log(f"{len(done)} days done, {len(todo)} to fetch (~{len(todo) * MIN_INTERVAL / 60:.0f} min)")

    t0 = time.time()
    last = 0.0
    for i, d in enumerate(todo, 1):
        wait = MIN_INTERVAL - (time.time() - last)
        if wait > 0:
            time.sleep(wait)
        last = time.time()
        res = fetch(d)
        with con:
            con.executemany(
                "INSERT OR REPLACE INTO bars VALUES (?,?,?,?,?,?,?,?,?)",
                [(d.isoformat(), x["T"], x.get("o"), x.get("h"), x.get("l"), x.get("c"),
                  x.get("v"), x.get("vw"), x.get("n")) for x in res if "T" in x])
            con.execute("INSERT OR REPLACE INTO days VALUES (?,?,?)",
                        (d.isoformat(), len(res), datetime.now().isoformat(timespec="seconds")))
        if i % 10 == 0 or i == len(todo):
            rate = (time.time() - t0) / i
            eta = (len(todo) - i) * rate / 60
            log(f"progress {i}/{len(todo)} (last {d}, {len(res)} tickers) ETA {eta:.0f} min")
    log("DONE")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"FAILED: {e}")
        sys.exit(1)

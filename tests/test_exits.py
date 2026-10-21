"""Exit-logic, stats and cleanup tests. Synthetic data only, no network, no real files touched.

    ./venv/bin/python tests/test_exits.py
"""
import csv
import json
import os
import shutil
import sys
import tempfile
import traceback
from datetime import datetime

import pandas as pd

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import account                          # noqa: E402
import performance_logger as pl         # noqa: E402
import self_improvement                 # noqa: E402
import trade_follower as tf             # noqa: E402
import one_time_archive_stale_signals as arch  # noqa: E402
from config import TIMEZONE             # noqa: E402

TMP = tempfile.mkdtemp(prefix="trading_tests_")
CSV = os.path.join(TMP, "trades.csv")

# Never let a test reach the real trades.csv, account.json or improvements.json
pl.TRADES_CSV = CSV
tf.TRADES_CSV = CSV
self_improvement.record_trade_result = lambda *a, **k: None
account.close_position = lambda *a, **k: 0.0
tf._tg = lambda text: None


def _reset_csv():
    if os.path.exists(CSV):
        os.remove(CSV)


def _rows():
    with open(CSV, newline="") as f:
        return list(csv.DictReader(f))


def _trade(**kw):
    t = {"symbol": "ABC", "signal": "STRONG BUY", "trade_type": "SWING TRADE",
         "entry_price": 100.0, "stop_loss": 95.0, "price_target": 110.0,
         "confirmed_at": "2026-10-01T10:00:00-04:00"}
    t.update(kw)
    return t


# ── _evaluate_exit ───────────────────────────────────────────────────────────

def test_long_exits():
    t = _trade()
    assert tf._evaluate_exit(t, 94.9) == "STOPPED"
    assert tf._evaluate_exit(t, 95.0) == "STOPPED"
    assert tf._evaluate_exit(t, 105.0) is None
    assert tf._evaluate_exit(t, 110.0) == "TARGET_HIT"
    assert tf._evaluate_exit(t, 120.0) == "TARGET_HIT"


def test_short_exits():
    t = _trade(signal="SHORT", stop_loss=105.0, price_target=90.0)
    assert tf._evaluate_exit(t, 105.5) == "STOPPED"
    assert tf._evaluate_exit(t, 95.0) is None
    assert tf._evaluate_exit(t, 89.0) == "TARGET_HIT"


def test_original_target_wins_over_raised_price_target():
    t = _trade(original_target=110.0, price_target=150.0)
    assert tf._evaluate_exit(t, 111.0) == "TARGET_HIT"


# ── _log_closed_trade ────────────────────────────────────────────────────────

def test_target_hit_logs_real_exit_price():
    _reset_csv()
    tf._log_closed_trade(_trade(original_target=110.0, price_target=150.0), 112.37, "TARGET_HIT")
    r = _rows()[0]
    assert r["status"] == "TARGET_HIT"
    assert float(r["exit_price"]) == 112.37
    assert float(r["target"]) == 110.0
    assert float(r["pnl_pct"]) == 12.37
    assert r["target_source"] == "original"


def test_short_pnl_sign_and_reconstructed_flag():
    _reset_csv()
    tf._log_closed_trade(_trade(signal="STRONG SELL", stop_loss=105.0, price_target=90.0,
                                target_reconstructed=True), 88.0, "TARGET_HIT")
    r = _rows()[0]
    assert float(r["pnl_pct"]) == 12.0
    assert r["target_source"] == "reconstructed"


# ── monitor loop (one tick, everything mocked) ───────────────────────────────

class _Stop(Exception):
    pass


def _run_loop(trade: dict, prices: list):
    """Run _monitor_loop_impl over the given 5m closes; return (store, calls)."""
    store = {trade["symbol"]: dict(trade)}
    calls = {"removed": [], "updates": []}
    prices = list(prices)

    class FakeDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 21, 11, 0, tzinfo=TIMEZONE)  # Wednesday, market open

    def fake_5m(sym):
        if not prices:
            raise _Stop()
        p = prices.pop(0)
        return pd.DataFrame({"Close": [p, p, p]})

    def update(sym, fields):
        calls["updates"].append(fields)
        if sym in store:
            store[sym].update(fields)

    def remove(sym):
        calls["removed"].append(sym)
        store.pop(sym, None)

    saved = (tf.datetime, tf.time.sleep, tf.load_trades, tf.update_trade_fields,
             tf.remove_trade, tf._get_5m_data, tf._get_1m_data, tf.yf)
    tf.datetime, tf.time.sleep = FakeDT, (lambda s: None)
    tf.load_trades, tf.update_trade_fields, tf.remove_trade = (lambda: store), update, remove
    tf._get_5m_data, tf._get_1m_data = fake_5m, (lambda s: pd.DataFrame())

    class _NoYF:
        class Ticker:
            def __init__(self, s):
                raise RuntimeError("no network in tests")
    tf.yf = _NoYF
    tf._active_threads[trade["symbol"]] = True
    try:
        tf._monitor_loop_impl(trade["symbol"])
    except _Stop:
        pass
    finally:
        (tf.datetime, tf.time.sleep, tf.load_trades, tf.update_trade_fields,
         tf.remove_trade, tf._get_5m_data, tf._get_1m_data, tf.yf) = saved
        tf._active_threads.pop(trade["symbol"], None)
    return store, calls


def test_loop_closes_on_target_and_never_raises_target():
    _reset_csv()
    store, calls = _run_loop(_trade(), [104.0, 108.0, 111.5, 130.0])
    assert calls["removed"] == ["ABC"]
    rows = _rows()
    assert len(rows) == 1 and rows[0]["status"] == "TARGET_HIT"
    assert float(rows[0]["exit_price"]) == 111.5
    for u in calls["updates"]:
        assert "price_target" not in u, u
    assert calls["updates"][0] == {"original_target": 110.0}


def test_loop_still_stops_out():
    _reset_csv()
    store, calls = _run_loop(_trade(), [101.0, 94.0])
    rows = _rows()
    assert rows[0]["status"] == "STOPPED" and float(rows[0]["exit_price"]) == 94.0
    assert calls["removed"] == ["ABC"]


def test_loop_keeps_frozen_original_target():
    _reset_csv()
    t = _trade(price_target=120.0, original_target=120.0, target_reconstructed=True)
    store, calls = _run_loop(t, [115.0, 119.0])
    assert calls["removed"] == [] and not os.path.exists(CSV)
    assert store["ABC"]["original_target"] == 120.0
    assert store["ABC"]["price_target"] == 120.0


# ── stats ────────────────────────────────────────────────────────────────────

def _row(status, pnl, src=""):
    return {"status": status, "pnl_pct": str(pnl), "target_source": src}


def test_wins_counted_by_pnl_not_status():
    rows = [_row("STOPPED", -3), _row("TARGET_HIT", 8), _row("MANUAL_CLOSE", 2),
            _row("MANUAL_CLOSE", -1), _row("OPEN", "")]
    s = pl.summarize(rows, {})
    assert s["clean"]["closed"] == 4
    assert s["clean"]["wins"] == 2
    assert abs(s["clean"]["avg_pnl"] - 1.5) < 1e-9
    assert s["open"] == 1


def test_reconstructed_kept_separate():
    rows = [_row("STOPPED", -4), _row("TARGET_HIT", 20, "reconstructed")]
    active = {"A": {"target_reconstructed": True}, "B": {}}
    s = pl.summarize(rows, active)
    assert s["clean"]["closed"] == 1 and s["clean"]["wins"] == 0
    assert s["recon"]["closed"] == 1 and s["recon"]["wins"] == 1
    assert s["live_clean"] == 1 and s["live_recon"] == ["A"]


def test_self_improvement_win_is_pnl_based():
    import importlib
    src = open(os.path.join(BASE, "self_improvement.py")).read()
    assert "is_win = pnl_pct > 0" in src


# ── nightly check_open_trades ────────────────────────────────────────────────

def _write_open_rows(rows):
    with open(CSV, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=pl.FIELDNAMES)
        w.writeheader()
        for r in rows:
            w.writerow(r)


def _open_row(sym, sig, entry, stop, target):
    return {"symbol": sym, "date": "2026-10-01", "signal_type": sig, "entry_price": entry,
            "stop": stop, "target": target, "status": "OPEN"}


def test_check_open_trades_uses_yfinance():
    _reset_csv()
    _write_open_rows([_open_row("UP", "BUY", 10, 9, 12), _open_row("DN", "BUY", 10, 9, 12),
                      _open_row("SH", "SHORT", 10, 11, 8), _open_row("NODATA", "BUY", 10, 9, 12),
                      _open_row("MID", "BUY", 10, 9, 12)])
    fake = {"UP": 12.5, "DN": 8.5, "SH": 7.5, "MID": 10.5}

    class FakeTicker:
        def __init__(self, s):
            self.s = s

        def history(self, **kw):
            return pd.DataFrame({"Close": [fake[self.s]]}) if self.s in fake else pd.DataFrame()

    saved = pl.yf.Ticker
    pl.yf.Ticker = FakeTicker
    try:
        pl.check_open_trades()
    finally:
        pl.yf.Ticker = saved
    by = {r["symbol"]: r for r in _rows()}
    assert by["UP"]["status"] == "TARGET_HIT" and float(by["UP"]["exit_price"]) == 12.5
    assert by["DN"]["status"] == "STOPPED" and float(by["DN"]["pnl_pct"]) == -15.0
    assert by["SH"]["status"] == "TARGET_HIT" and float(by["SH"]["pnl_pct"]) == 25.0
    assert by["NODATA"]["status"] == "OPEN"
    assert by["MID"]["status"] == "OPEN"
    assert "polygon" not in open(pl.__file__).read().lower()


# ── one-time cleanup script ──────────────────────────────────────────────────

OLD_HEADER = pl.FIELDNAMES[:-1]


def test_archive_moves_only_stale_open_rows_and_is_idempotent():
    d = tempfile.mkdtemp(dir=TMP)
    tcsv, acsv = os.path.join(d, "trades.csv"), os.path.join(d, "archive.csv")
    rows = [
        {"symbol": "OLD1", "date": "2026-05-07", "status": "OPEN", "entry_price": "1"},
        {"symbol": "OLD1", "date": "2026-05-07", "status": "OPEN", "entry_price": "1"},
        {"symbol": "OLD2", "date": "2026-05-11", "status": "OPEN", "entry_price": "2"},
        {"symbol": "KEEP", "date": "2026-05-21", "status": "STOPPED", "pnl_pct": "-3.18",
         "exit_price": "8.210000038146973"},
    ]
    with open(tcsv, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=OLD_HEADER)
        w.writeheader()
        w.writerows(rows)

    assert arch.archive_stale_rows(tcsv, acsv) == 3
    with open(tcsv, newline="") as f:
        reader = csv.DictReader(f)
        kept = list(reader)
        assert reader.fieldnames == pl.FIELDNAMES
    assert [r["symbol"] for r in kept] == ["KEEP"]
    assert kept[0]["exit_price"] == "8.210000038146973" and kept[0]["pnl_pct"] == "-3.18"
    with open(acsv, newline="") as f:
        archived = list(csv.DictReader(f))
    assert [r["symbol"] for r in archived] == ["OLD1", "OLD1", "OLD2"]

    before = open(tcsv).read(), open(acsv).read()
    assert arch.archive_stale_rows(tcsv, acsv) == 0
    assert (open(tcsv).read(), open(acsv).read()) == before


def test_fix_active_trades():
    d = tempfile.mkdtemp(dir=TMP)
    p = os.path.join(d, "active.json")
    json.dump({"HIW": {"price_target": 36.8}, "URI": {"price_target": 1222.07},
               "NEW": {"price_target": 5.0, "original_target": 4.0}}, open(p, "w"))
    res = arch.fix_active_trades(p)
    assert res == {"removed": True, "frozen": ["URI"]}
    data = json.load(open(p))
    assert "HIW" not in data
    assert data["URI"]["original_target"] == 1222.07 and data["URI"]["target_reconstructed"] is True
    assert data["NEW"]["original_target"] == 4.0 and "target_reconstructed" not in data["NEW"]
    assert arch.fix_active_trades(p) == {"removed": False, "frozen": []}


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS {name}")
        except Exception:
            failed += 1
            print(f"FAIL {name}")
            traceback.print_exc()
    shutil.rmtree(TMP, ignore_errors=True)
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)

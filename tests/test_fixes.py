"""Tests for the 2026-10-26 fixes: scanner switches, ATR exit rules, risk-neutral sizing,
time exit and the swing 3/day cap. Synthetic data only, no network, no real files written.

    ./venv/bin/python tests/test_fixes.py
"""
import json
import os
import sys
import traceback
import types
from datetime import date, datetime, timedelta

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import config                           # noqa: E402
import exit_rules as er                 # noqa: E402
import scheduler                        # noqa: E402
import swing_scanner                    # noqa: E402
import trade_follower as tf             # noqa: E402
from config import TIMEZONE             # noqa: E402

# Never let a test send, save or log anything real
SENT, SAVED = [], []
scheduler.send_trade_alert = lambda r: SENT.append(r["symbol"])
scheduler.save_pending_alert = lambda r: SAVED.append(dict(r))
scheduler.pt_log_alert = lambda *a, **k: None
scheduler.is_daily_limit_hit = lambda: False
scheduler.check_correlation = lambda sym: {}
scheduler._enrich_result = lambda r: r
scheduler.get_position_size = lambda conviction: 1000.0
scheduler.close_yf_thread_cache = lambda: None
er.daily_atr = lambda symbol, n=14: 0.0          # no network: missing ATR stays missing
tf._tg = lambda text: None


def _reset():
    SENT.clear(); SAVED.clear()
    scheduler._alerted_today = set()
    scheduler._alerted_date = date.today()


def _alert(sym="ABC", scanner="DAY_BREAKOUT", price=100.0, stop=95.0, atr=2.0, signal="STRONG BUY"):
    return {"symbol": sym, "scanner": scanner, "price": price, "stop_loss": stop,
            "price_target": price * 1.3, "atr": atr, "signal": signal, "conviction": "HIGH", "score": 90}


def _risk(r):
    return r["position_size"] / r["price"] * abs(r["price"] - r["stop_loss"])


# ── 1. scanner switches / Telegram ────────────────────────────────────────────

def test_day_news_and_open_scan_are_off():
    assert config.SCANNERS_ENABLED["DAY_NEWS"] is False
    assert config.SCANNERS_ENABLED["OPEN_SCAN"] is False
    for code_still_there in ("day_scanner.py", "open_drive.py"):
        assert os.path.exists(os.path.join(BASE, code_still_there))


def test_disabled_scanner_alert_is_dropped():
    _reset()
    for sc in ("DAY_NEWS", "OPEN_SCAN"):
        assert scheduler._emit_alert(_alert(sym=sc[:3], scanner=sc)) is False
    assert SENT == [] and SAVED == [] and scheduler._alerted_today == set()


def test_enabled_scanner_alert_goes_through():
    _reset()
    assert scheduler._emit_alert(_alert(scanner="DAY_BREAKOUT")) is True
    assert [r["symbol"] for r in SAVED] == ["ABC"]


def test_all_telegram_trade_alerts_off():
    assert config.TELEGRAM_ALERTS_ENABLED and not any(config.TELEGRAM_ALERTS_ENABLED.values())
    _reset()
    scheduler._emit_alert(_alert(scanner="SWING"))
    assert SENT == []                    # saved/logged, never sent


# ── 2. exit rules ─────────────────────────────────────────────────────────────

def test_tight_long_stop_widened_to_1_5_atr_and_target_2_5_atr():
    r = _alert(price=100.0, stop=99.0, atr=2.0)
    assert er.apply_exit_rules(r)
    assert r["stop_loss"] == 97.0 and r["price_target"] == 105.0
    assert r["original_stop"] == 99.0 and r["max_hold_days"] == er.MAX_HOLD_DAYS == 10


def test_tight_short_stop_widened_mirror():
    r = _alert(price=50.0, stop=50.5, atr=1.0, signal="SHORT")
    assert er.apply_exit_rules(r)
    assert r["stop_loss"] == 51.5 and r["price_target"] == 47.5


def test_wide_stop_left_alone():
    r = _alert(price=100.0, stop=90.0, atr=2.0)
    assert er.apply_exit_rules(r)
    assert r["stop_loss"] == 90.0 and r["risk_scale"] == 1.0 and r["price_target"] == 105.0


def test_no_atr_alert_is_skipped():
    _reset()
    assert er.apply_exit_rules(_alert(atr=0)) is False
    assert scheduler._emit_alert(_alert(atr=0)) is False
    assert SAVED == [] and scheduler._alerted_today == set()


# ── dollar risk unchanged when the stop is widened ────────────────────────────

def test_widened_stop_keeps_dollar_risk():
    _reset()
    # scanner stop 1% away -> old 1.5% floor -> 98.5 ; ATR 2 -> new stop 97.0
    before = {"price": 100.0, "stop_loss": 98.5, "position_size": 1000.0}
    assert scheduler._emit_alert(_alert(price=100.0, stop=99.0, atr=2.0)) is True
    r = SAVED[-1]
    assert r["stop_loss"] == 97.0
    assert r["position_size"] == 500.0
    assert abs(_risk(r) - _risk(before)) < 1e-9        # $15 both ways


def test_wide_stop_keeps_full_position():
    _reset()
    assert scheduler._emit_alert(_alert(price=100.0, stop=90.0, atr=2.0)) is True
    assert SAVED[-1]["position_size"] == 1000.0


def test_scale_position_never_grows():
    assert er.scale_position(1000.0, 1.7) == 1000.0
    assert er.scale_position(1000.0, 0.25) == 250.0


# ── time exit (new trades only) ───────────────────────────────────────────────

ENTRY = TIMEZONE.localize(datetime(2026, 10, 26, 10, 0)).isoformat()      # Monday


def _at(y, m, d, hh, mm):
    return TIMEZONE.localize(datetime(y, m, d, hh, mm))


def test_trading_day_count_skips_weekends():
    assert er.trading_day_number(date(2026, 10, 26), date(2026, 10, 26)) == 1
    assert er.trading_day_number(date(2026, 10, 26), date(2026, 11, 6)) == 10     # Fri of week 2


def test_time_exit_fires_at_close_of_day_10_not_before():
    t = {"entry_time": ENTRY, "max_hold_days": 10}
    assert not er.time_exit_due(t, _at(2026, 11, 5, 15, 55))      # day 9
    assert not er.time_exit_due(t, _at(2026, 11, 6, 15, 49))      # day 10, before 15:50
    assert er.time_exit_due(t, _at(2026, 11, 6, 15, 50))          # day 10, near close
    assert er.time_exit_due(t, _at(2026, 11, 9, 9, 31))           # missed -> next session


def test_old_trades_never_time_exit():
    old = {"entry_time": "2026-05-19T13:32:13-04:00"}               # no max_hold_days
    assert not er.time_exit_due(old, _at(2027, 5, 1, 15, 55))
    with open(os.path.join(BASE, "active_trades.json")) as f:       # read-only
        live = json.load(f)
    assert live and not any(er.time_exit_due(t, _at(2027, 5, 1, 15, 55)) for t in live.values())


def test_new_alert_carries_max_hold_to_pending_and_trade():
    import active_trades, telegram_bot
    src = open(active_trades.__file__).read() + open(telegram_bot.__file__).read()
    assert '"max_hold_days": result.get("max_hold_days")' in src
    assert 'trade["max_hold_days"] = pending["max_hold_days"]' in src


def test_stop_and_target_messages_still_sent():
    src = open(tf.__file__).read()
    assert "STOP HIT — EXIT NOW" in src and "TARGET HIT — EXIT" in src


# ── 3. swing 3/day cap ────────────────────────────────────────────────────────

class _SyncThread:
    def __init__(self, target, **kw):
        self.target = target

    def start(self):
        self.target()


def _swing_setup(sym):
    return {"symbol": sym, "scanner": "SWING", "signal": "STRONG BUY", "score": 100,
            "price": 50.0, "stop_loss": 47.0, "price_target": 60.0, "atr": 2.0,
            "conviction": "SPECULATIVE"}


def test_swing_cap_three_per_day_across_scans():
    _reset()
    swing_scanner.run_and_release = lambda fn, s: fn(s)
    swing_scanner.analyze_swing_stock = _swing_setup
    swing_scanner._swing_alerted_date = None
    real_threading = scheduler.threading
    scheduler.threading = types.SimpleNamespace(Thread=_SyncThread)   # scheduler's name only
    try:
        for k in range(5):                                   # 5 scans, 4 new symbols each
            scheduler._last_swing_scan = None
            scheduler._run_swing_scan([f"S{k}{i}" for i in range(4)], {})
        assert len(SAVED) == 3, [r["symbol"] for r in SAVED]
        assert swing_scanner._swing_count_today == 3 and swing_scanner.daily_limit_reached()

        # next day the cap resets
        swing_scanner._swing_alerted_date = date.today() - timedelta(days=1)
        scheduler._alerted_date = date.today() - timedelta(days=1)
        scheduler._last_swing_scan = None
        scheduler._run_swing_scan(["N1", "N2", "N3", "N4"], {})
        assert len(SAVED) == 6
    finally:
        scheduler.threading = real_threading


def test_swing_blocked_alert_does_not_use_a_slot():
    _reset()
    swing_scanner.run_and_release = lambda fn, s: fn(s)
    swing_scanner.analyze_swing_stock = _swing_setup
    swing_scanner._swing_alerted_date = None
    scheduler._alerted_today = {"DUP"}                       # already alerted by another scanner
    real_threading = scheduler.threading
    scheduler.threading = types.SimpleNamespace(Thread=_SyncThread)   # scheduler's name only
    try:
        scheduler._last_swing_scan = None
        scheduler._run_swing_scan(["DUP"], {})
        assert swing_scanner._swing_count_today == 0
    finally:
        scheduler.threading = real_threading


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
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)

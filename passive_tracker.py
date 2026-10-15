"""Passive tracker — logs every alert to tracking.csv and checks price outcomes.

Checks at: 1 hour after alert, end of day (3:55 PM), 1 trading day later, 3 trading days later.
Feeds outcomes into self_improvement.py.
"""
import csv
import fcntl
import json
import os
import threading
import time
from contextlib import contextmanager
from datetime import datetime, date, timedelta

import yfinance as yf

from config import TRACKING_CSV, TIMEZONE
from data_fetcher import get_polygon_bars

_lock = threading.Lock()
_bg_thread = None

# threading.Lock only protects against races between threads in THIS process.
# tracking.csv also gets written by standalone one-time scripts (e.g. the path-based
# backfill) running as separate OS processes, so every read-modify-write cycle also
# takes an flock on a sibling lock file — real cross-process protection, not just an
# in-process guard.
_FLOCK_PATH = TRACKING_CSV + ".lock"


@contextmanager
def _cross_process_lock():
    with open(_FLOCK_PATH, "w") as lockfile:
        fcntl.flock(lockfile, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lockfile, fcntl.LOCK_UN)

FIELDNAMES = [
    "symbol", "alert_date", "alert_time", "alert_price",
    "signal_type", "scanner", "conviction", "regime",
    "stop_loss", "price_target",
    "price_1h", "price_eod", "price_1d", "price_3d",
    "pnl_1h_pct", "pnl_eod_pct", "pnl_1d_pct", "pnl_3d_pct",
    "outcome_1h", "outcome_eod", "outcome_1d", "outcome_3d",
    "was_confirmed",
    # Path-based outcome fields (item 2): computed from Polygon 1-min bars walked
    # from entry forward, instead of trusting a single point-in-time price snapshot.
    "hit_stop", "hit_target", "first_hit", "max_favorable_pct", "max_adverse_pct",
    # Data-quality flag (item 3): never deleted, just marked for exclusion in analysis.
    "data_suspect",
    # Item 4: the numeric score and every component sub-score, going forward only —
    # the score was never computed retroactively for historical rows, so there is
    # nothing to backfill here (unlike price data, it isn't recoverable from Polygon).
    "score", "score_components",
]

MAX_ADVERSE_PCT_OUTLIER = 50.0
MIN_SANE_ENTRY_PRICE = 1.0


def _ensure_csv():
    if not os.path.exists(TRACKING_CSV):
        with open(TRACKING_CSV, "w", newline="") as f:
            csv.DictWriter(f, fieldnames=FIELDNAMES).writeheader()


def _get_price(symbol: str) -> float:
    try:
        info = yf.Ticker(symbol).fast_info
        return float(getattr(info, "last_price", 0) or 0)
    except Exception:
        return 0.0


def _is_short(signal_type: str) -> bool:
    return signal_type in ("SHORT", "STRONG SELL", "SELL")


def _outcome(pnl_pct: float, stop_pct: float) -> str:
    if pnl_pct >= 5.0:
        return "WIN"
    if pnl_pct <= -stop_pct:
        return "STOP"
    if abs(pnl_pct) < 2.0:
        return "FLAT"
    return "LOSS" if pnl_pct < 0 else "GAIN"


def _trading_day_offset(from_date: date, days: int) -> date:
    d = from_date
    added = 0
    while added < days:
        d += timedelta(days=1)
        if d.weekday() < 5:
            added += 1
    return d


def _checkpoint_deadline(alert_dt: datetime, alert_date: date, horizon: str):
    if horizon == "1h":
        return alert_dt + timedelta(hours=1)
    if horizon == "eod":
        return TIMEZONE.localize(datetime.combine(alert_date, datetime.min.time())).replace(hour=15, minute=55)
    if horizon == "1d":
        d = _trading_day_offset(alert_date, 1)
        return TIMEZONE.localize(datetime.combine(d, datetime.min.time())).replace(hour=15, minute=55)
    if horizon == "3d":
        d = _trading_day_offset(alert_date, 3)
        return TIMEZONE.localize(datetime.combine(d, datetime.min.time())).replace(hour=15, minute=55)
    raise ValueError(horizon)


def path_analysis(bars: list, entry_price: float, stop_loss: float, price_target: float,
                   is_short: bool) -> dict:
    """Walk 1-min bars chronologically from entry and determine whether the stop or
    the target was crossed FIRST. Also tracks the best/worst excursion along the way.

    Returns dict: hit_stop, hit_target, first_hit ('stop'/'target'/''),
    first_hit_time (datetime or None), first_hit_price, max_favorable_pct, max_adverse_pct.
    """
    result = {
        "hit_stop": False, "hit_target": False, "first_hit": "",
        "first_hit_time": None, "first_hit_price": None,
        "max_favorable_pct": 0.0, "max_adverse_pct": 0.0,
    }
    if not bars or not entry_price:
        return result

    max_fav = 0.0
    max_adv = 0.0
    for bar in bars:
        t = datetime.fromtimestamp(bar["t"] / 1000, tz=TIMEZONE)
        hi, lo = bar.get("h"), bar.get("l")
        if hi is None or lo is None:
            continue

        if is_short:
            fav_pct = (entry_price - lo) / entry_price * 100
            adv_pct = (hi - entry_price) / entry_price * 100
        else:
            fav_pct = (hi - entry_price) / entry_price * 100
            adv_pct = (entry_price - lo) / entry_price * 100
        max_fav = max(max_fav, fav_pct)
        max_adv = max(max_adv, adv_pct)

        stop_touched = (hi >= stop_loss) if is_short else (lo <= stop_loss)
        target_touched = (lo <= price_target) if is_short else (hi >= price_target)

        if result["first_hit"] == "" and (stop_touched or target_touched):
            # Both crossed in the same bar: can't tell intrabar order from OHLC alone.
            # Conservative convention: assume the stop went first.
            if stop_touched:
                result["hit_stop"] = True
                result["first_hit"] = "stop"
                result["first_hit_time"] = t
                result["first_hit_price"] = stop_loss
            else:
                result["hit_target"] = True
                result["first_hit"] = "target"
                result["first_hit_time"] = t
                result["first_hit_price"] = price_target
        elif result["first_hit"] == "stop" and target_touched:
            result["hit_target"] = True
        elif result["first_hit"] == "target" and stop_touched:
            result["hit_stop"] = True

    result["max_favorable_pct"] = round(max_fav, 2)
    result["max_adverse_pct"] = round(max_adv, 2)
    return result


def _data_suspect_check(row: dict, entry_bars: list = None) -> bool:
    """Flag (don't delete) rows whose numbers look like data artifacts rather than
    real, tradeable moves: sub-$1 entries (penny stocks/warrants with near-zero
    liquidity) and >50% swings (often stale/bad quotes, thin trading, or corporate
    actions). If entry_bars (Polygon bars starting at/near alert time) are supplied,
    also cross-checks the logged alert_price against Polygon's own price at that
    same moment — reuses the bars already fetched for path analysis rather than
    spending a second rate-limited API call on the same row."""
    try:
        entry_price = float(row.get("alert_price", 0) or 0)
    except (ValueError, TypeError):
        entry_price = 0.0

    if entry_price and entry_price < MIN_SANE_ENTRY_PRICE:
        return True

    for h in ("1h", "eod", "1d", "3d"):
        raw = row.get(f"pnl_{h}_pct", "")
        if raw == "" or raw is None:
            continue
        try:
            if abs(float(raw)) > MAX_ADVERSE_PCT_OUTLIER:
                return True
        except (ValueError, TypeError):
            continue

    if entry_bars and entry_price:
        poly_price = entry_bars[0].get("c") or entry_bars[0].get("o")
        if poly_price and abs(poly_price - entry_price) / entry_price > 0.20:
            return True

    return False


def log_alert(result: dict, regime: str = "UNKNOWN") -> None:
    _ensure_csv()
    now = datetime.now(TIMEZONE)
    stop = float(result.get("stop_loss", 0))
    entry = float(result.get("price", 0))
    stop_pct = abs(entry - stop) / entry * 100 if entry else 3.0

    row = {
        "symbol":       result.get("symbol", ""),
        "alert_date":   now.date().isoformat(),
        "alert_time":   now.strftime("%H:%M:%S"),
        "alert_price":  float(result.get("price", 0)),
        "signal_type":  result.get("signal", ""),
        "scanner":      result.get("scanner", ""),
        "conviction":   result.get("conviction", "SPECULATIVE"),
        "regime":       regime,
        "stop_loss":    stop,
        "price_target": float(result.get("price_target", 0)),
        "price_1h":     "",
        "price_eod":    "",
        "price_1d":     "",
        "price_3d":     "",
        "pnl_1h_pct":   "",
        "pnl_eod_pct":  "",
        "pnl_1d_pct":   "",
        "pnl_3d_pct":   "",
        "outcome_1h":   "",
        "outcome_eod":  "",
        "outcome_1d":   "",
        "outcome_3d":   "",
        "was_confirmed": "no",
        "hit_stop":     "",
        "hit_target":   "",
        "first_hit":    "",
        "max_favorable_pct": "",
        "max_adverse_pct":   "",
        "data_suspect": entry < MIN_SANE_ENTRY_PRICE if entry else False,
        "score":            result.get("score", ""),
        "score_components": json.dumps(result.get("score_components", {})),
    }
    with _lock, _cross_process_lock():
        with open(TRACKING_CSV, "a", newline="") as f:
            csv.DictWriter(f, fieldnames=FIELDNAMES).writerow(row)


def mark_confirmed(symbol: str, alert_date: str) -> None:
    _ensure_csv()
    _update_rows(symbol, alert_date, {"was_confirmed": "yes"})


def _update_rows(symbol: str, alert_date: str, updates: dict) -> None:
    with _lock, _cross_process_lock():
        try:
            with open(TRACKING_CSV, "r", newline="") as f:
                rows = list(csv.DictReader(f))
            for row in rows:
                if row["symbol"] == symbol and row["alert_date"] == alert_date:
                    row.update(updates)
            with open(TRACKING_CSV, "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=FIELDNAMES)
                w.writeheader()
                w.writerows(rows)
        except Exception as e:
            print(f"[Tracker] Update error: {e}")


MAX_ROWS_PER_PASS = 25  # bounds how long one _check_and_fill() call can block on the
                        # 5-req/min Polygon rate limit; anything left over is picked
                        # up on the next pass 5 minutes later (same as before).


def _price_at_or_before(bars: list, deadline: datetime):
    chosen = None
    for bar in bars:
        t = datetime.fromtimestamp(bar["t"] / 1000, tz=TIMEZONE)
        if t <= deadline:
            chosen = bar
        else:
            break
    if chosen is None and bars:
        chosen = bars[0]
    return chosen.get("c") if chosen else None


def _row_needs_checkpoint(row: dict, alert_date: date, alert_dt: datetime, today: date, now: datetime) -> bool:
    if not row.get("price_1h") and (now - alert_dt).total_seconds() / 60 >= 60:
        return True
    if not row.get("price_eod") and alert_date == today and now >= now.replace(hour=15, minute=55, second=0, microsecond=0):
        return True
    if not row.get("price_1d"):
        next_day = _trading_day_offset(alert_date, 1)
        if today >= next_day and now.hour >= 15 and now.minute >= 55:
            return True
    if not row.get("price_3d"):
        day3 = _trading_day_offset(alert_date, 3)
        if today >= day3 and now.hour >= 15 and now.minute >= 55:
            return True
    return False


def _check_and_fill_locked() -> None:
    with open(TRACKING_CSV, "r", newline="") as f:
        rows = list(csv.DictReader(f))

    now    = datetime.now(TIMEZONE)
    today  = now.date()
    changed = False
    processed = 0

    for row in rows:
        try:
            alert_date_str = row.get("alert_date", "")
            alert_time_str = row.get("alert_time", "00:00:00")
            if not alert_date_str:
                continue

            alert_date = date.fromisoformat(alert_date_str)
            alert_dt   = datetime.combine(alert_date, datetime.strptime(alert_time_str, "%H:%M:%S").time())
            alert_dt   = TIMEZONE.localize(alert_dt)

            if not _row_needs_checkpoint(row, alert_date, alert_dt, today, now):
                continue
            if processed >= MAX_ROWS_PER_PASS:
                continue  # leave for the next pass, 5 min from now

            symbol      = row["symbol"]
            alert_price = float(row.get("alert_price", 0) or 0)
            stop_loss   = float(row.get("stop_loss", 0) or 0)
            price_target = float(row.get("price_target", 0) or 0)
            is_short    = _is_short(row.get("signal_type", ""))
            stop_pct    = abs(alert_price - stop_loss) / alert_price * 100 if alert_price and stop_loss else 3.0

            # One Polygon fetch covers the whole path from entry through the
            # furthest-out checkpoint we still need — reused below for every
            # horizon instead of one call per horizon.
            day3_deadline = _checkpoint_deadline(alert_dt, alert_date, "3d")
            fetch_to = min(now, day3_deadline + timedelta(hours=1))
            bars = get_polygon_bars(symbol, alert_dt, fetch_to) if alert_price else []
            processed += 1

            path = path_analysis(bars, alert_price, stop_loss, price_target, is_short) if bars else None
            if path:
                row["hit_stop"]          = "yes" if path["hit_stop"] else "no"
                row["hit_target"]        = "yes" if path["hit_target"] else "no"
                row["first_hit"]         = path["first_hit"]
                row["max_favorable_pct"] = path["max_favorable_pct"]
                row["max_adverse_pct"]   = path["max_adverse_pct"]
                changed = True

            for h in ("1h", "eod", "1d", "3d"):
                if row.get(f"price_{h}"):
                    continue
                deadline = _checkpoint_deadline(alert_dt, alert_date, h)
                if now < deadline:
                    continue

                price = pnl = outcome = None

                # Stop/target crossed before this checkpoint's deadline: that IS
                # the outcome, at that price, even if the price later recovered.
                if path and path["first_hit"] and path["first_hit_time"] and path["first_hit_time"] <= deadline:
                    if path["first_hit"] == "stop":
                        price, outcome = stop_loss, "STOP"
                    else:
                        price, outcome = price_target, "WIN"
                    if price and alert_price:
                        pnl = round((price - alert_price) / alert_price * 100 * (-1 if is_short else 1), 2)
                else:
                    # Nothing hit yet by this deadline: read the actual price
                    # from the historical bar path (not a "right now" snapshot).
                    bar_price = _price_at_or_before(bars, deadline) if bars else None
                    if bar_price and alert_price:
                        price = bar_price
                        pnl   = round((price - alert_price) / alert_price * 100 * (-1 if is_short else 1), 2)
                        outcome = _outcome(pnl, stop_pct)
                    else:
                        # Polygon has no bars yet for this window (e.g. this very
                        # minute) — fall back to a live quote so a due checkpoint
                        # doesn't stall indefinitely.
                        live = _get_price(symbol)
                        if live and alert_price:
                            price = live
                            pnl   = round((price - alert_price) / alert_price * 100 * (-1 if is_short else 1), 2)
                            outcome = _outcome(pnl, stop_pct)

                if price is not None:
                    row[f"price_{h}"]   = price
                    row[f"pnl_{h}_pct"] = pnl
                    row[f"outcome_{h}"] = outcome
                    _feed_improvement(row, h, pnl)
                    changed = True

            row["data_suspect"] = _data_suspect_check(row, entry_bars=bars)
            changed = True

        except Exception:
            continue

    if changed:
        with open(TRACKING_CSV, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=FIELDNAMES)
            w.writeheader()
            w.writerows(rows)


def _check_and_fill() -> None:
    _ensure_csv()
    try:
        # Cross-process lock held for the whole read-modify-write cycle (not just
        # the read/write ends) so a concurrent writer — e.g. the path-based backfill
        # script, a separate OS process — can't have its update clobbered by our
        # stale in-memory copy, and vice versa.
        with _lock, _cross_process_lock():
            _check_and_fill_locked()
    except Exception as e:
        print(f"[Tracker] Check error: {e}")


def _feed_improvement(row: dict, checkpoint: str, pnl_pct: float) -> None:
    try:
        from self_improvement import record_passive_outcome
        record_passive_outcome(
            scanner    = row.get("scanner", ""),
            conviction = row.get("conviction", "SPECULATIVE"),
            regime     = row.get("regime", "UNKNOWN"),
            alert_time = row.get("alert_time", ""),
            pnl_pct    = pnl_pct,
            checkpoint = checkpoint,
            confirmed  = row.get("was_confirmed", "no") == "yes",
        )
    except Exception:
        pass


def get_today_alerts() -> list:
    _ensure_csv()
    today = date.today().isoformat()
    try:
        with _lock, _cross_process_lock():
            with open(TRACKING_CSV, "r", newline="") as f:
                rows = list(csv.DictReader(f))
        return [r for r in rows if r.get("alert_date") == today]
    except Exception:
        return []


def _bg_loop() -> None:
    while True:
        try:
            now = datetime.now(TIMEZONE)
            if now.weekday() < 5 and 6 <= now.hour < 20:
                _check_and_fill()
        except Exception as e:
            print(f"[Tracker] BG error: {e}")
        time.sleep(300)


def start_tracker() -> None:
    global _bg_thread
    if _bg_thread and _bg_thread.is_alive():
        return
    _bg_thread = threading.Thread(target=_bg_loop, daemon=True, name="PassiveTracker")
    _bg_thread.start()
    print("[PassiveTracker] Started background tracker")

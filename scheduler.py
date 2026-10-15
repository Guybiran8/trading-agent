"""Main scheduler — coordinates all scanners, timers, and daily events."""
import time
import threading
import yfinance as yf
from datetime import datetime, timedelta, date

from config import TELEGRAM_ALERTS_ENABLED
from config import (
    TIMEZONE, NEWS_SCAN_INTERVAL, DAY_SCAN_INTERVAL,
    SWING_SCAN_INTERVAL, PREMARKET_SCAN_INTERVAL,
)
from market_regime import detect_regime, mark_news_driven, regime_summary
from news_scanner import scan_news
from data_fetcher import get_all_symbols, close_yf_thread_cache
from telegram_bot import start_bot
from telegram_notifier import (
    send_morning_summary, send_trade_alert, send_weekly_report,
    send_premarket_news_digest, send_eod_summary, send_daily_limit_message,
)
from active_trades import save_pending_alert
from trade_follower import resume_monitoring_on_startup
from account import get_position_size, ensure_account_file, get_summary, get_daily_pnl
from performance_logger import check_open_trades, print_performance_summary
from self_improvement import run_weekly_analysis, should_run_weekly, get_best_time_buckets
from passive_tracker import log_alert as pt_log_alert, start_tracker, get_today_alerts
from sector_rotation import run_sector_rotation, get_sector_score, get_all_rankings, is_stale
from daily_limiter import is_daily_limit_hit, get_daily_pnl as limiter_pnl, check_correlation

_wakening_running = False
_last_open_scan: date = None

# ── State ─────────────────────────────────────────────────────────────────────
_alerted_today: set = set()
_alerted_date: date = None
_alerted_lock = threading.Lock()
_regime_today: str = "CHOPPY"

_swing_running  = False
_short_running  = False
_ah_running     = False

_last_news_scan         = None
_last_day_scan          = None
_last_swing_scan        = None
_last_premarket_scan    = None
_last_morning_summary   = None
_last_premarket_digest  = None
_last_sector_rotation   = None
_last_eod_summary       = None
_last_eod_check         = None
_last_weekly_check      = None
_last_earnings_scan: date = None
_daily_limit_notified: date = None


def _reset_if_new_day():
    global _alerted_today, _alerted_date, _daily_limit_notified
    today = date.today()
    if _alerted_date != today:
        _alerted_today          = set()
        _alerted_date           = today
        _daily_limit_notified   = None


def get_current_session() -> str:
    now  = datetime.now(TIMEZONE)
    h, m = now.hour, now.minute
    if now.weekday() >= 5:
        return "weekend"
    if h < 6:
        return "closed"
    if h < 8:
        return "early_premarket"
    if h < 9 or (h == 9 and m < 15):
        return "premarket"
    if h == 9 and 15 <= m < 25:
        return "sector_prep"
    if h == 9 and 25 <= m < 30:
        return "morning_prep"
    if h < 16:
        return "market_open"
    if h < 20:
        return "afterhours"
    return "closed"


def _seconds_until_next_session() -> int:
    now = datetime.now(TIMEZONE)
    if now.weekday() < 5:
        target = now.replace(hour=6, minute=0, second=0, microsecond=0)
        if now < target:
            return int((target - now).total_seconds())
    days_ahead = 1
    if now.weekday() == 4:
        days_ahead = 3
    elif now.weekday() == 5:
        days_ahead = 2
    elif now.weekday() == 6:
        days_ahead = 1
    nxt = (now + timedelta(days=days_ahead)).replace(hour=6, minute=0, second=0, microsecond=0)
    return max(60, int((nxt - now).total_seconds()))


def _enrich_result(result: dict) -> dict:
    """Add sector score, insider buying, dark pool flags to a result."""
    sym = result.get("symbol", "")
    components = result.setdefault("score_components", {})

    # Sector score
    try:
        sector_bonus, sector_name, sector_rank = get_sector_score(sym)
        result["sector"]       = sector_name
        result["sector_rank"]  = sector_rank
        result["score"]        = result.get("score", 0) + sector_bonus
        components["sector_rank_bonus"] = sector_bonus
    except Exception:
        pass

    # Insider buying
    try:
        from insider_detector import get_insider_flag
        has_ins, ins_detail = get_insider_flag(sym)
        result["insider_buying"] = has_ins
        result["insider_detail"] = ins_detail
        components["insider_bonus"] = 25 if has_ins else 0
        if has_ins:
            result["score"] = result.get("score", 0) + 25
            result.setdefault("flags", []).append(f"INSIDER BUYING")
    except Exception:
        pass

    # Dark pool
    try:
        from dark_pool import get_dark_pool_flag
        has_dp, dp_detail = get_dark_pool_flag(sym)
        result["dark_pool"]        = has_dp
        result["dark_pool_detail"] = dp_detail
        components["dark_pool_bonus"] = 30 if has_dp else 0
        if has_dp:
            result["score"] = result.get("score", 0) + 30
            result.setdefault("flags", []).append("DARK POOL ACTIVITY")
    except Exception:
        pass

    return result


def _emit_alert(result: dict) -> None:
    global _alerted_today, _daily_limit_notified
    _reset_if_new_day()
    sym = result["symbol"]

    with _alerted_lock:
        if sym in _alerted_today:
            return
        _alerted_today.add(sym)

    # Minimum stop distance check (1.5% for longs, 1.5% for shorts)
    em_price = result.get("price", 0)
    em_stop  = result.get("stop_loss", 0)
    if em_price and em_stop:
        em_stop_pct = abs(em_price - em_stop) / em_price * 100
        if em_stop_pct < 0.5:
            print(f"  SKIPPED (stop invalid {em_stop_pct:.2f}%): {sym}")
            with _alerted_lock:
                _alerted_today.discard(sym)
            return
        if em_stop_pct < 1.5:
            em_signal = result.get("signal", "")
            if em_signal not in ("SHORT", "STRONG SELL", "SELL"):
                result["stop_loss"] = round(em_price * 0.985, 2)
            else:
                result["stop_loss"] = round(em_price * 1.015, 2)

    # Daily loss limit check
    if is_daily_limit_hit():
        today = date.today()
        if _daily_limit_notified != today:
            _daily_limit_notified = today
            summary = get_summary()
            send_daily_limit_message(get_daily_pnl(), summary["balance"])
        print(f"  BLOCKED (daily limit): {sym}")
        return

    # Enrich with sector, insider, dark pool
    result = _enrich_result(result)

    # Correlation warning
    try:
        corr = check_correlation(sym)
        if corr.get("has_warn"):
            level = corr["level"]
            open_syms = corr.get("open_syms", [])
            sector    = corr.get("sector", "")
            if level == "double":
                result["correlation_warning"] = (
                    f"HIGH SECTOR CONCENTRATION ({sector}) — "
                    f"already have {open_syms}. Consider sizing smaller."
                )
            else:
                result["correlation_warning"] = (
                    f"NOTE — already have {open_syms[0]} open in {sector}"
                )
    except Exception:
        pass

    # Position size
    conviction = result.get("conviction", "SPECULATIVE")
    result["position_size"] = get_position_size(conviction)

    if TELEGRAM_ALERTS_ENABLED.get(result.get("scanner", ""), True):
        send_trade_alert(result)
    save_pending_alert(result)

    # Log to passive tracker
    try:
        pt_log_alert(result, _regime_today)
    except Exception:
        pass

    print(f"  ALERT: {sym} | {result.get('scanner')} | Score {result.get('score')} | {conviction}")


def _run_news_scan(regime: dict) -> dict:
    global _last_news_scan
    now = datetime.now(TIMEZONE)
    if _last_news_scan and (now - _last_news_scan).total_seconds() < NEWS_SCAN_INTERVAL - 5:
        return regime
    result = scan_news()
    _last_news_scan = now
    if result.get("is_major_macro"):
        return mark_news_driven(regime)
    return regime


def _run_day_scan(symbols: list, regime: dict) -> None:
    global _last_day_scan
    _reset_if_new_day()
    now = datetime.now(TIMEZONE)
    if _last_day_scan and (now - _last_day_scan).total_seconds() < DAY_SCAN_INTERVAL - 5:
        return
    _last_day_scan = now

    from day_scanner import run_day_scanner
    with _alerted_lock:
        alerted_copy = set(_alerted_today)
    alerts = run_day_scanner(symbols, alerted_copy)
    for a in alerts:
        _emit_alert(a)


def _run_swing_scan(symbols: list, regime: dict) -> None:
    global _last_swing_scan, _swing_running
    now = datetime.now(TIMEZONE)
    if _last_swing_scan and (now - _last_swing_scan).total_seconds() < SWING_SCAN_INTERVAL - 10:
        return
    if _swing_running:
        return
    _last_swing_scan = now

    def _bg():
        global _swing_running
        _swing_running = True
        try:
            from swing_scanner import run_swing_scanner
            _reset_if_new_day()
            with _alerted_lock:
                alerted_copy = set(_alerted_today)
            alerts = run_swing_scanner(symbols, alerted_copy)
            for a in alerts:
                _emit_alert(a)
        except Exception as e:
            print(f"[SwingScanner] Error: {e}")
        finally:
            _swing_running = False
            close_yf_thread_cache()

    threading.Thread(target=_bg, daemon=True, name="SwingScanner").start()


def _run_short_scan(symbols: list, regime: dict) -> None:
    global _short_running
    if regime.get("regime") != "BEAR" or _short_running:
        return

    def _bg():
        global _short_running
        _short_running = True
        try:
            from short_scanner import run_short_scanner
            _reset_if_new_day()
            with _alerted_lock:
                alerted_copy = set(_alerted_today)
            alerts = run_short_scanner(symbols, alerted_copy)
            for a in alerts:
                _emit_alert(a)
        except Exception as e:
            print(f"[ShortScanner] Error: {e}")
        finally:
            _short_running = False
            close_yf_thread_cache()

    threading.Thread(target=_bg, daemon=True, name="ShortScanner").start()


def _run_volume_awakening(symbols: list) -> None:
    global _wakening_running
    if _wakening_running:
        return

    def _bg():
        global _wakening_running
        _wakening_running = True
        try:
            from volume_awakening import run_volume_awakening
            _reset_if_new_day()
            with _alerted_lock:
                alerted_copy = set(_alerted_today)
            alerts = run_volume_awakening(symbols, alerted_copy)
            for a in alerts:
                _emit_alert(a)
        except Exception as e:
            print(f"[VolumeAwakening] Error: {e}")
        finally:
            _wakening_running = False
            close_yf_thread_cache()

    import threading
    threading.Thread(target=_bg, daemon=True, name="VolumeAwakening").start()


def _run_afterhours_scan(symbols: list) -> None:
    global _ah_running
    if _ah_running:
        return

    def _bg():
        global _ah_running
        _ah_running = True
        try:
            from afterhours_scanner import run_afterhours_scanner
            _reset_if_new_day()
            with _alerted_lock:
                alerted_copy = set(_alerted_today)
            alerts = run_afterhours_scanner(symbols, alerted_copy)
            for a in alerts:
                _emit_alert(a)
        except Exception as e:
            print(f"[AHScanner] Error: {e}")
        finally:
            _ah_running = False
            close_yf_thread_cache()

    threading.Thread(target=_bg, daemon=True, name="AHScanner").start()


def _run_premarket_scan(symbols: list, regime: dict) -> None:
    global _last_premarket_scan
    now = datetime.now(TIMEZONE)
    if _last_premarket_scan and (now - _last_premarket_scan).total_seconds() < PREMARKET_SCAN_INTERVAL - 10:
        return
    _last_premarket_scan = now

    def _bg():
        try:
            from premarket_scanner import run_premarket_scanner
            alerts = run_premarket_scanner(symbols, regime.get("regime", "CHOPPY"))
            for a in alerts:
                _emit_alert(a)
        except Exception as e:
            print(f"[PremarketScanner] Error: {e}")
        finally:
            close_yf_thread_cache()

    threading.Thread(target=_bg, daemon=True, name="PremarketScanner").start()


def _do_sector_rotation():
    global _last_sector_rotation
    today = date.today()
    if _last_sector_rotation == today:
        return
    try:
        run_sector_rotation()
        _last_sector_rotation = today
    except Exception as e:
        print(f"[SectorRotation] Error: {e}")


def _get_futures() -> dict:
    futures_map = {"ES (S&P500)": "ES=F", "NQ (Nasdaq)": "NQ=F", "YM (Dow)": "YM=F"}
    result = {}
    for name, ticker in futures_map.items():
        try:
            df = yf.Ticker(ticker).history(period="2d", interval="1d")
            if df is not None and len(df) >= 2:
                prev  = float(df["Close"].iloc[-2])
                curr  = float(df["Close"].iloc[-1])
                result[name] = round((curr - prev) / prev * 100, 2) if prev else 0
        except Exception:
            pass
    return result


def _get_global_markets() -> dict:
    mkts = {"FTSE": "^FTSE", "DAX": "^GDAXI", "Nikkei": "^N225", "Hang Seng": "^HSI"}
    result = {}
    for name, ticker in mkts.items():
        try:
            df = yf.Ticker(ticker).history(period="2d", interval="1d")
            if df is not None and len(df) >= 2:
                prev  = float(df["Close"].iloc[-2])
                curr  = float(df["Close"].iloc[-1])
                result[name] = round((curr - prev) / prev * 100, 2) if prev else 0
        except Exception:
            pass
    return result


def _send_premarket_digest(news_result: dict, pm_alerts: list) -> None:
    global _last_premarket_digest
    today = date.today()
    if _last_premarket_digest == today:
        return
    try:
        futures     = _get_futures()
        global_mkt  = _get_global_markets()
        events      = news_result.get("events", []) if news_result else []
        send_premarket_news_digest(futures, global_mkt, events, pm_alerts)
        _last_premarket_digest = today
        print("[Scheduler] Pre-market digest sent")
    except Exception as e:
        print(f"[Scheduler] Digest error: {e}")


def _send_morning_summary(regime: dict, news_result: dict) -> None:
    global _last_morning_summary
    today = date.today()
    if _last_morning_summary == today:
        return
    try:
        sector_rankings = get_all_rankings()
        best_windows    = get_best_time_buckets()

        top_sectors = []
        for s in sector_rankings[:4]:
            if s["pct_1d"] > 0.2:
                top_sectors.append(f"{s['name']} ({s['pct_1d']:+.1f}%)")

        events = news_result.get("events", []) if news_result else []
        send_morning_summary(regime, top_sectors, events, sector_rankings, best_windows)
        _last_morning_summary = today
        print(f"[Scheduler] Morning summary sent — {regime.get('regime')} regime")
    except Exception as e:
        print(f"[Scheduler] Morning summary error: {e}")


def _send_eod_summary(regime: str) -> None:
    global _last_eod_summary
    today = date.today()
    if _last_eod_summary == today:
        return
    try:
        alerts_today = get_today_alerts()
        summary      = get_summary()
        daily_pnl    = get_daily_pnl()
        send_eod_summary(regime, alerts_today, daily_pnl, summary["balance"])
        _last_eod_summary = today
        print("[Scheduler] EOD summary sent")
    except Exception as e:
        print(f"[Scheduler] EOD summary error: {e}")


def run_auto_scheduler() -> None:
    global _last_eod_check, _last_weekly_check, _regime_today, _last_earnings_scan, _last_open_scan

    ensure_account_file()

    print("Trading Agent — Enhanced Build")
    print(f"  Time:    {datetime.now(TIMEZONE).strftime('%Y-%m-%d %I:%M %p ET')}")
    print(f"  Session: {get_current_session()}")
    print()

    start_bot()
    resume_monitoring_on_startup()
    start_tracker()

    symbols             = None
    regime              = {"regime": "CHOPPY"}
    last_regime_refresh = None

    while True:
        now     = datetime.now(TIMEZONE)
        session = get_current_session()
        today   = now.date()

        # ── CLOSED / WEEKEND ─────────────────────────────────────────────────
        if session in ("closed", "weekend"):
            wait = _seconds_until_next_session()
            h, m = wait // 3600, (wait % 3600) // 60
            print(f"{now.strftime('%I:%M %p ET')} — Closed. Next in {h}h {m}m")

            if _last_eod_check != today and now.hour >= 20:
                check_open_trades()
                print_performance_summary()
                _last_eod_check = today

            if _last_weekly_check != today and now.weekday() == 6 and should_run_weekly():
                report = run_weekly_analysis()
                send_weekly_report(report)
                _last_weekly_check = today

            time.sleep(min(wait, 1800))
            symbols = None
            continue

        # ── LOAD SYMBOLS once per day ─────────────────────────────────────────
        if symbols is None:
            print("[Scheduler] Loading symbol universe...")
            symbols = get_all_symbols()

        # ── REFRESH REGIME every 15 min ───────────────────────────────────────
        if last_regime_refresh is None or (now - last_regime_refresh).total_seconds() > 900:
            try:
                regime = detect_regime(force_refresh=True)
                _regime_today = regime.get("regime", "CHOPPY")
                print(f"[Regime] {regime_summary(regime)}")
                last_regime_refresh = now
            except Exception as e:
                print(f"[Regime] Error: {e}")

        # ── EARLY PRE-MARKET 6:00–8:00 AM ────────────────────────────────────
        if session == "early_premarket":
            try:
                regime = _run_news_scan(regime)
            except Exception:
                pass
            time.sleep(60)
            continue

        # ── PRE-MARKET 8:00–9:15 AM ───────────────────────────────────────────
        if session == "premarket":
            # 8:00 AM: digest + first pre-market alerts
            if now.hour == 8 and now.minute < 2:
                news_result = scan_news()
                pm_alerts   = []
                try:
                    from premarket_scanner import run_premarket_scanner
                    pm_alerts = run_premarket_scanner(symbols, regime.get("regime", "CHOPPY"))
                    for a in pm_alerts:
                        _emit_alert(a)
                except Exception as e:
                    print(f"[PremarketScanner] {e}")
                _send_premarket_digest(news_result, pm_alerts)

            # Continuing pre-market: news scan + pre-market scan
            try:
                regime = _run_news_scan(regime)
            except Exception:
                pass
            _run_premarket_scan(symbols, regime)
            time.sleep(60)
            continue

        # ── SECTOR ROTATION at 9:00-9:15 AM ──────────────────────────────────
        if session == "sector_prep":
            _do_sector_rotation()
            try:
                regime = _run_news_scan(regime)
            except Exception:
                pass
            # Second pre-market alert sweep at 9:15 AM
            _run_premarket_scan(symbols, regime)
            time.sleep(30)
            continue

        # ── MORNING PREP 9:25 AM ──────────────────────────────────────────────
        if session == "morning_prep":
            if _last_sector_rotation != today:
                _do_sector_rotation()
            if _last_morning_summary != today:
                try:
                    news_result = scan_news()
                    if news_result.get("is_major_macro"):
                        regime = mark_news_driven(regime)
                    _send_morning_summary(regime, news_result)
                    _last_news_scan = now
                except Exception as e:
                    print(f"[Morning] Error: {e}")
            time.sleep(10)
            continue

        # ── MARKET HOURS 9:30–4:00 PM ────────────────────────────────────────
        if session == "market_open":
            # Refresh sector rotation if stale
            if is_stale():
                _do_sector_rotation()

            # Volume Awakening — highest priority, every 60s, 9:30-15:30
            _run_volume_awakening(symbols)

            # 9:30 AM open scan (first minute only)
            if now.hour == 9 and now.minute == 30 and _last_open_scan != today:
                def _open_scan_bg():
                    global _last_open_scan
                    try:
                        from open_drive import run_open_scan
                        _reset_if_new_day()
                        with _alerted_lock:
                            alerted_copy = set(_alerted_today)
                        alerts = run_open_scan(symbols, alerted_copy)
                        for a in alerts:
                            _emit_alert(a)
                    except Exception as e:
                        print(f"[OpenScan] Error: {e}")
                    finally:
                        close_yf_thread_cache()
                import threading as _t
                _t.Thread(target=_open_scan_bg, daemon=True, name="OpenScan").start()
                _last_open_scan = today

            # 9:45 AM open drive check
            try:
                from open_drive import run_open_drive
                with _alerted_lock:
                    alerted_copy = set(_alerted_today)
                od_alerts = run_open_drive(symbols, alerted_copy)
                for a in od_alerts:
                    _emit_alert(a)
            except Exception as e:
                pass

            # News scan
            try:
                regime = _run_news_scan(regime)
                _regime_today = regime.get("regime", "CHOPPY")
            except Exception as e:
                print(f"[News] Error: {e}")

            # Day scanner every 60s
            try:
                _run_day_scan(symbols, regime)
            except Exception as e:
                print(f"[DayScanner] Error: {e}")

            # Swing scanner every 30 min (background)
            _run_swing_scan(symbols, regime)

            # Short scanner — BEAR regime only
            if regime.get("regime") == "BEAR":
                _run_short_scan(symbols, regime)

            # Earnings scan once per day (10:00 AM)
            if _last_earnings_scan != today and now.hour == 10 and now.minute < 2:
                def _earn_bg():
                    try:
                        from earnings_intel import run_earnings_scan
                        run_earnings_scan(symbols)
                    except Exception as e:
                        print(f"[Earnings] Error: {e}")
                    finally:
                        close_yf_thread_cache()
                threading.Thread(target=_earn_bg, daemon=True, name="EarningsScan").start()
                _last_earnings_scan = today

            # EOD summary at 4:15 PM
            eod_time = now.replace(hour=16, minute=15, second=0, microsecond=0)
            if now >= eod_time and _last_eod_summary != today:
                _send_eod_summary(_regime_today)

            time.sleep(DAY_SCAN_INTERVAL)
            continue

        # ── AFTER-HOURS 4:00–8:00 PM ─────────────────────────────────────────
        if session == "afterhours":
            try:
                regime = _run_news_scan(regime)
            except Exception:
                pass

            _run_afterhours_scan(symbols)

            # EOD summary at 4:15 PM (catch if market session missed it)
            eod_time = now.replace(hour=16, minute=15, second=0, microsecond=0)
            if now >= eod_time and _last_eod_summary != today:
                _send_eod_summary(_regime_today)

            if _last_eod_check != today and now.hour >= 17:
                check_open_trades()
                print_performance_summary()
                _last_eod_check = today

            time.sleep(60)
            continue

        time.sleep(30)

"""Trade follower — real-time monitoring for confirmed trades.

Uses 5-minute candles for exit decisions. Checks every 60 seconds.
Never exits in first 10 minutes after entry. Handles all exit conditions.
"""
import csv
import os
import threading
import time
from datetime import datetime, date, timedelta

import pandas as pd
import requests
import yfinance as yf

from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TIMEZONE, TRADES_CSV
from active_trades import load_trades, remove_trade, update_trade_fields
from data_fetcher import close_yf_thread_cache
from performance_logger import FIELDNAMES

_active_threads: dict = {}
_active_lock = threading.Lock()


def _tg(text: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"},
            timeout=10,
        )
    except Exception:
        pass


def _get_5m_data(symbol: str) -> pd.DataFrame:
    try:
        df = yf.Ticker(symbol).history(period="5d", interval="5m")
        return df if df is not None and not df.empty else pd.DataFrame()
    except Exception:
        return pd.DataFrame()


def _get_1m_data(symbol: str) -> pd.DataFrame:
    try:
        df = yf.Ticker(symbol).history(period="1d", interval="1m")
        return df if df is not None and not df.empty else pd.DataFrame()
    except Exception:
        return pd.DataFrame()


def _ensure_header_matches() -> bool:
    """Returns True if the file needs (re)writing (missing or header mismatched)."""
    if not os.path.exists(TRADES_CSV):
        return True
    with open(TRADES_CSV, newline="") as f:
        first_line = f.readline().strip()
    return first_line != ",".join(FIELDNAMES)


def _is_short(trade: dict) -> bool:
    return trade.get("signal", "") in ("SHORT", "STRONG SELL", "SELL")


def _exit_target(trade: dict) -> float:
    """The target an exit is judged against: the one set at entry, never a raised one."""
    return float(trade.get("original_target") or trade.get("price_target") or 0)


def _evaluate_exit(trade: dict, price: float):
    """Return "STOPPED", "TARGET_HIT" or None for the current price. Stop is checked first."""
    stop     = float(trade.get("stop_loss", 0) or 0)
    target   = _exit_target(trade)
    is_short = _is_short(trade)
    if stop and ((is_short and price >= stop) or (not is_short and price <= stop)):
        return "STOPPED"
    if target and ((is_short and price <= target) or (not is_short and price >= target)):
        return "TARGET_HIT"
    return None


def _log_closed_trade(trade: dict, exit_price: float, status: str) -> None:
    entry  = float(trade.get("entry_price", 0))
    is_short = trade.get("signal", "") in ("SHORT", "STRONG SELL", "SELL")
    pnl    = round((exit_price - entry) / entry * 100 * (-1 if is_short else 1), 2) if entry else 0

    needs_header = _ensure_header_matches()
    if needs_header and os.path.exists(TRADES_CSV):
        # Existing file has a stale/mismatched header (e.g. from an older schema).
        # Rewrite just the header in place; data rows already match FIELDNAMES order —
        # see one_time_fix_trades_csv.py for the historical repair of pre-schema-change rows.
        with open(TRADES_CSV, newline="") as f:
            lines = f.readlines()
        lines[0] = ",".join(FIELDNAMES) + "\n"
        with open(TRADES_CSV, "w", newline="") as f:
            f.writelines(lines)
        needs_header = False

    with open(TRADES_CSV, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDNAMES)
        if needs_header:
            w.writeheader()
        w.writerow({
            "symbol":      trade["symbol"],
            "date":        str(trade.get("confirmed_at", ""))[:10],
            "signal_type": trade.get("signal", ""),
            "trade_type":  trade.get("trade_type", ""),
            "scanner":     trade.get("scanner", ""),
            "entry_price": entry,
            "stop":        trade.get("stop_loss", 0),
            "target":      _exit_target(trade),
            "conviction":  trade.get("conviction", "SPECULATIVE"),
            "status":      status,
            "exit_price":  exit_price,
            "exit_date":   date.today().isoformat(),
            "pnl_pct":     pnl,
            "target_source": "reconstructed" if trade.get("target_reconstructed") else "original",
        })

    # Trigger self-improvement recording
    try:
        from self_improvement import record_trade_result
        record_trade_result(trade, exit_price, pnl, status)
    except Exception:
        pass

    # Update account balance
    try:
        from account import close_position
        is_s = trade.get("signal", "") in ("SHORT", "STRONG SELL", "SELL")
        close_position(trade["symbol"], exit_price, is_s)
    except Exception:
        pass


def _volume_dead_10m(df_1m: pd.DataFrame) -> bool:
    """Check if volume has been dead for 10 consecutive minutes."""
    if df_1m.empty or len(df_1m) < 10:
        return False
    try:
        last10 = df_1m["Volume"].tail(10)
        avg    = float(df_1m["Volume"].mean())
        return all(float(v) < avg * 0.2 for v in last10)
    except Exception:
        return False


def _monitor_loop(symbol: str) -> None:
    try:
        _monitor_loop_impl(symbol)
    finally:
        close_yf_thread_cache()


def _monitor_loop_impl(symbol: str) -> None:
    print(f"[TradeFollower] Started watching {symbol}")

    ema20_alerted   = False
    close_warned    = False
    volume_alerted  = False
    momentum_alerted = False

    # Track entry time for 10-minute no-exit rule
    entry_time = None
    try:
        trades = load_trades()
        if symbol in trades:
            entry_str = trades[symbol].get("entry_time", "")
            if entry_str:
                entry_time = datetime.fromisoformat(entry_str)
            # Freeze the entry target so later new highs can't move the exit level
            if not trades[symbol].get("original_target"):
                update_trade_fields(symbol, {"original_target": trades[symbol].get("price_target", 0)})
    except Exception:
        pass

    while True:
        with _active_lock:
            if not _active_threads.get(symbol):
                break

        time.sleep(60)

        now = datetime.now(TIMEZONE)
        if now.weekday() >= 5 or now.hour < 4 or now.hour >= 20:
            continue

        trades = load_trades()
        if symbol not in trades:
            print(f"[TradeFollower] {symbol} removed — stopping")
            break
        t = trades[symbol]

        # Never exit in first 10 minutes after entry
        if entry_time:
            try:
                elapsed = (now - entry_time.replace(tzinfo=now.tzinfo)).total_seconds() / 60
                if elapsed < 10:
                    continue
            except Exception:
                pass

        # Get 5-minute candles for exit decisions
        df_5m = _get_5m_data(symbol)
        if df_5m.empty or len(df_5m) < 3:
            continue

        price    = float(df_5m["Close"].iloc[-1])
        entry    = float(t.get("entry_price", 0))
        stop     = float(t.get("stop_loss", 0))
        target   = _exit_target(t)
        is_short = t.get("signal", "") in ("SHORT", "STRONG SELL", "SELL")
        trade_type = t.get("trade_type", "SWING TRADE")
        new_high = float(t.get("new_high", entry) or entry)

        pnl = round((price - entry) / entry * 100 * (-1 if is_short else 1), 2) if entry else 0

        exit_status = _evaluate_exit(t, price)

        # ── STOP HIT ─────────────────────────────────────────────────────────
        if exit_status == "STOPPED":
            _tg(
                f"🛑 *STOP HIT — EXIT NOW: {symbol}*\n"
                f"Price: ${price:.2f} | Stop: ${stop:.2f}\n"
                f"P&L: {pnl:+.2f}%"
            )
            _log_closed_trade(t, price, "STOPPED")
            remove_trade(symbol)
            break

        # ── TARGET HIT ───────────────────────────────────────────────────────
        if exit_status == "TARGET_HIT":
            _tg(
                f"🎯 *TARGET HIT — EXIT: {symbol}*\n"
                f"Price: ${price:.2f} | Target: ${target:.2f}\n"
                f"P&L: {pnl:+.2f}%"
            )
            _log_closed_trade(t, price, "TARGET_HIT")
            remove_trade(symbol)
            break

        # ── 50% GIVE-BACK (intraday momentum dead) ───────────────────────────
        if not is_short and not momentum_alerted and new_high and entry and new_high > entry * 1.005:
            move_from_entry = new_high - entry
            give_back = new_high - price
            if give_back > move_from_entry * 0.5 and pnl > 0:
                _tg(
                    f"📉 *MOMENTUM DEAD — consider exit: {symbol}*\n"
                    f"Gave back >50% of intraday move\n"
                    f"High: ${new_high:.2f} | Now: ${price:.2f} | P&L: {pnl:+.2f}%"
                )
                momentum_alerted = True

        # ── NEW HIGH ─────────────────────────────────────────────────────────
        if not is_short and price > new_high * 1.005:
            update_trade_fields(symbol, {"new_high": price})
            new_high = price

        # ── VOLUME DEAD 10 CONSECUTIVE MINUTES (day trades only) ─────────────
        if trade_type == "DAY TRADE" and not volume_alerted:
            df_1m = _get_1m_data(symbol)
            if _volume_dead_10m(df_1m):
                _tg(
                    f"📊 *VOLUME DEAD — MOVE OVER: {symbol}*\n"
                    f"Zero volume for 10+ consecutive minutes\n"
                    f"Price: ${price:.2f} | P&L: {pnl:+.2f}% | Consider exit"
                )
                volume_alerted = True

        # ── 3:45 PM WARNING (day trades) ─────────────────────────────────────
        if trade_type == "DAY TRADE" and not close_warned:
            mkt_close = now.replace(hour=16, minute=0, second=0, microsecond=0)
            mins_left = (mkt_close - now).total_seconds() / 60
            if 13 <= mins_left <= 17:
                _tg(
                    f"⏰ *CLOSE NOW — MARKET CLOSES IN 15 MIN: {symbol}*\n"
                    f"Price: ${price:.2f} | P&L: {pnl:+.2f}%\n"
                    f"_Exit now. Day trades must close by 4:00 PM ET._"
                )
                close_warned = True

        # ── MACRO RISK: SPY drops hard ───────────────────────────────────────
        try:
            spy = yf.Ticker("SPY").history(period="1d", interval="5m")
            if not spy.empty:
                spy_recent = float(spy["Close"].iloc[-1])
                spy_open   = float(spy["Open"].iloc[0])
                spy_drop   = (spy_recent - spy_open) / spy_open * 100
                if spy_drop < -1.5 and not is_short and pnl > -5:
                    _tg(
                        f"🌊 *MACRO RISK — SPY DOWN {spy_drop:.1f}%: {symbol}*\n"
                        f"Consider reducing exposure. Price: ${price:.2f} | P&L: {pnl:+.2f}%"
                    )
        except Exception:
            pass

    with _active_lock:
        _active_threads.pop(symbol, None)
    print(f"[TradeFollower] Stopped watching {symbol}")


def start_monitoring(symbol: str, trade: dict) -> None:
    with _active_lock:
        if symbol in _active_threads:
            return
        _active_threads[symbol] = True
    t = threading.Thread(target=_monitor_loop, args=(symbol,), daemon=True, name=f"Follow-{symbol}")
    t.start()


def stop_monitoring(symbol: str) -> None:
    with _active_lock:
        _active_threads[symbol] = False


def resume_monitoring_on_startup() -> None:
    trades = load_trades()
    for sym, trade in trades.items():
        start_monitoring(sym, trade)
    if trades:
        print(f"[TradeFollower] Resumed {len(trades)} trade(s): {list(trades.keys())}")

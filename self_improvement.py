"""Self-improvement engine — records trade results, tracks passive outcomes, adjusts scoring weekly."""
import json
import os
import threading
from datetime import datetime, date
from config import IMPROVEMENTS_FILE, RECOMMENDATIONS_FILE, TIMEZONE

# Auto-apply is frozen: weights in improvements.json are no longer mutated by
# run_weekly_analysis(). Recommendations are written to RECOMMENDATIONS_FILE for a
# human to review and apply manually. See changelog for why (measurement was unreliable
# — SHORT alerts had inverted P&L, point-in-time outcome checks, unfiltered penny-stock
# outliers — so weight changes were being driven by bad data).
AUTO_APPLY_WEIGHTS = False


def _save_recommendations(entries: list) -> None:
    with _lock:
        tmp = RECOMMENDATIONS_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"generated": datetime.now(TIMEZONE).isoformat(), "recommendations": entries}, f, indent=2)
        os.replace(tmp, RECOMMENDATIONS_FILE)

_lock = threading.Lock()

TIME_BUCKETS = [
    "0930-1000", "1000-1030", "1030-1100", "1100-1130",
    "1130-1200", "1200-1230", "1230-1300", "1300-1330",
    "1330-1400", "1400-1430", "1430-1500", "1500-1530", "1530-1600",
]


def _default_data() -> dict:
    return {
        "weights": {
            "volume_multiplier":   1.0,
            "news_bonus":          1.0,
            "freshness_multiplier": 1.0,
            "sector_bonus":        1.0,
            "rs_bonus":            1.0,
            "accumulation_bonus":  1.0,
            "pullback_bonus":      1.0,
            "insider_bonus":       1.0,
            "dark_pool_bonus":     1.0,
        },
        "performance": {
            "by_signal_type": {
                "breakout":    {"wins": 0, "losses": 0, "total_pnl": 0.0},
                "news_driven": {"wins": 0, "losses": 0, "total_pnl": 0.0},
                "swing":       {"wins": 0, "losses": 0, "total_pnl": 0.0},
                "short":       {"wins": 0, "losses": 0, "total_pnl": 0.0},
                "premarket":   {"wins": 0, "losses": 0, "total_pnl": 0.0},
                "afterhours":  {"wins": 0, "losses": 0, "total_pnl": 0.0},
            },
            "by_time": {
                "early": {"wins": 0, "losses": 0},
                "mid":   {"wins": 0, "losses": 0},
                "late":  {"wins": 0, "losses": 0},
            },
            "by_time_bucket": {b: {"wins": 0, "losses": 0} for b in TIME_BUCKETS},
            "by_regime": {
                "BULL":        {"wins": 0, "losses": 0},
                "BEAR":        {"wins": 0, "losses": 0},
                "CHOPPY":      {"wins": 0, "losses": 0},
                "NEWS_DRIVEN": {"wins": 0, "losses": 0},
            },
            "by_sector":    {},
            "by_conviction": {
                "HIGH":        {"wins": 0, "losses": 0},
                "SPECULATIVE": {"wins": 0, "losses": 0},
            },
            "passive": {
                "total":   0,
                "win_5pct": 0,
                "stopped":  0,
                "flat":     0,
                "by_scanner": {},
            },
        },
        "adjustments":         [],
        "last_weekly_report":  "",
        "confirmed_trade_adjustments": [],
    }


def _load() -> dict:
    if os.path.exists(IMPROVEMENTS_FILE):
        try:
            with open(IMPROVEMENTS_FILE) as f:
                data = json.load(f)
            # Migrate: add missing keys
            default = _default_data()
            perf = data.setdefault("performance", {})
            perf.setdefault("by_time_bucket", default["performance"]["by_time_bucket"])
            perf.setdefault("passive", default["performance"]["passive"])
            data.setdefault("weights", default["weights"])
            for k, v in default["weights"].items():
                data["weights"].setdefault(k, v)
            return data
        except Exception:
            pass
    return _default_data()


def _save(data: dict) -> None:
    with _lock:
        tmp = IMPROVEMENTS_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, IMPROVEMENTS_FILE)


def _classify_signal_type(trade: dict) -> str:
    scanner = trade.get("scanner", "").upper()
    if "NEWS" in scanner:
        return "news_driven"
    if "PREMARKET" in scanner:
        return "premarket"
    if "AFTERHOURS" in scanner or "AH" in scanner:
        return "afterhours"
    if "BREAKOUT" in scanner or "DAY" in scanner:
        return "breakout"
    if "SHORT" in scanner:
        return "short"
    return "swing"


def _classify_time(trade: dict) -> str:
    try:
        entry_str = trade.get("entry_time") or trade.get("confirmed_at", "")
        entry_dt  = datetime.fromisoformat(entry_str)
        if entry_dt.tzinfo is None:
            entry_dt = entry_dt.replace(tzinfo=datetime.now(TIMEZONE).tzinfo)
        entry_et = entry_dt.astimezone(TIMEZONE)
        market_min = (entry_et.hour - 9) * 60 + entry_et.minute - 30
        if market_min < 60:
            return "early"
        if market_min < 240:
            return "mid"
        return "late"
    except Exception:
        return "mid"


def _classify_time_bucket(trade: dict) -> str:
    try:
        entry_str = trade.get("entry_time") or trade.get("confirmed_at", "")
        entry_dt  = datetime.fromisoformat(entry_str)
        if entry_dt.tzinfo is None:
            entry_dt = entry_dt.replace(tzinfo=datetime.now(TIMEZONE).tzinfo)
        entry_et = entry_dt.astimezone(TIMEZONE)
        if entry_et.hour < 9 or entry_et.hour >= 16:
            return "0930-1000"
        market_min  = (entry_et.hour - 9) * 60 + entry_et.minute - 30
        if market_min < 0:
            market_min = 0
        bucket_idx  = min(market_min // 30, len(TIME_BUCKETS) - 1)
        return TIME_BUCKETS[bucket_idx]
    except Exception:
        return "0930-1000"


def record_trade_result(trade: dict, exit_price: float, pnl_pct: float, status: str) -> None:
    data   = _load()
    perf   = data["performance"]
    is_win = status in ("TARGET_HIT", "MANUAL_CLOSE") and pnl_pct > 0

    sig_type   = _classify_signal_type(trade)
    time_slot  = _classify_time(trade)
    time_bucket = _classify_time_bucket(trade)
    conviction = trade.get("conviction", "SPECULATIVE")

    st = perf["by_signal_type"].setdefault(sig_type, {"wins": 0, "losses": 0, "total_pnl": 0.0})
    if is_win:
        st["wins"] += 1
    else:
        st["losses"] += 1
    st["total_pnl"] = round(float(st.get("total_pnl", 0)) + pnl_pct, 2)

    ts = perf["by_time"].setdefault(time_slot, {"wins": 0, "losses": 0})
    if is_win:
        ts["wins"] += 1
    else:
        ts["losses"] += 1

    tb = perf["by_time_bucket"].setdefault(time_bucket, {"wins": 0, "losses": 0})
    if is_win:
        tb["wins"] += 1
    else:
        tb["losses"] += 1

    cv = perf["by_conviction"].setdefault(conviction, {"wins": 0, "losses": 0})
    if is_win:
        cv["wins"] += 1
    else:
        cv["losses"] += 1

    # Record daily P&L for loss limiter
    try:
        from daily_limiter import record_trade_pnl
        entry = float(trade.get("entry_price", 0))
        size  = float(trade.get("position_size", 1000))
        shares = size / entry if entry else 0
        pnl_dollar = (exit_price - entry) * shares
        is_short = trade.get("signal", "") in ("SHORT", "STRONG SELL")
        if is_short:
            pnl_dollar = -pnl_dollar
        record_trade_pnl(pnl_dollar)
    except Exception:
        pass

    _save(data)


def record_passive_outcome(scanner: str, conviction: str, regime: str,
                           alert_time: str, pnl_pct: float,
                           checkpoint: str, confirmed: bool) -> None:
    data = _load()
    passive = data["performance"].setdefault("passive", {
        "total": 0, "win_5pct": 0, "stopped": 0, "flat": 0, "by_scanner": {}
    })
    passive["total"] = passive.get("total", 0) + 1
    if pnl_pct >= 5.0:
        passive["win_5pct"] = passive.get("win_5pct", 0) + 1
    elif pnl_pct < -2.0:
        passive["stopped"] = passive.get("stopped", 0) + 1
    else:
        passive["flat"] = passive.get("flat", 0) + 1

    sc = passive.setdefault("by_scanner", {}).setdefault(scanner, {"total": 0, "wins": 0})
    sc["total"] += 1
    if pnl_pct >= 5.0:
        sc["wins"] += 1

    _save(data)


def run_weekly_analysis() -> dict:
    data   = _load()
    perf   = data["performance"]
    now    = datetime.now(TIMEZONE)
    adjustments = []

    def win_rate(d):
        total = d.get("wins", 0) + d.get("losses", 0)
        return d.get("wins", 0) / total if total >= 3 else None

    # Adjust weights by signal type
    for sig_type, stats in perf["by_signal_type"].items():
        wr = win_rate(stats)
        if wr is None:
            continue
        total_pnl = float(stats.get("total_pnl", 0))

        if sig_type == "news_driven":
            if wr >= 0.65 or total_pnl > 5:
                adjustments.append(f"[recommend] news_bonus +0.1 (news_driven {wr*100:.0f}% win rate)")
            elif wr < 0.35:
                adjustments.append(f"[recommend] news_bonus -0.1 (news_driven {wr*100:.0f}% win rate)")

        elif sig_type == "breakout":
            if wr >= 0.65:
                adjustments.append(f"[recommend] volume_multiplier +0.1 (breakout {wr*100:.0f}% win rate)")
            elif wr < 0.35:
                adjustments.append(f"[recommend] volume_multiplier -0.1 (breakout {wr*100:.0f}% win rate)")

        elif sig_type == "swing":
            if wr >= 0.65:
                adjustments.append(f"[recommend] accumulation_bonus +0.1 (swing {wr*100:.0f}% win rate)")
            elif wr < 0.35:
                adjustments.append(f"[recommend] accumulation_bonus -0.1 (swing {wr*100:.0f}% win rate)")

    # Identify best time bucket
    bucket_rates = {}
    for bucket, stats in perf.get("by_time_bucket", {}).items():
        wr = win_rate(stats)
        if wr is not None:
            bucket_rates[bucket] = wr

    if bucket_rates:
        best_bucket  = max(bucket_rates, key=bucket_rates.get)
        worst_bucket = min(bucket_rates, key=bucket_rates.get)
        adjustments.append(f"Best time window: {best_bucket} ({bucket_rates[best_bucket]*100:.0f}% win rate)")
        adjustments.append(f"Weakest window: {worst_bucket} ({bucket_rates[worst_bucket]*100:.0f}% win rate) — raise threshold")

    # Passive outcome analysis
    passive = perf.get("passive", {})
    p_total = passive.get("total", 0)
    p_wins  = passive.get("win_5pct", 0)
    if p_total >= 10:
        p_wr = p_wins / p_total
        adjustments.append(f"Passive alerts: {p_total} tracked, {p_wr*100:.0f}% hit 5%+ gain")

    # Best performing scanner (passive)
    for sc_name, sc_stats in passive.get("by_scanner", {}).items():
        sc_total = sc_stats.get("total", 0)
        sc_wins  = sc_stats.get("wins", 0)
        if sc_total >= 5:
            sc_wr = sc_wins / sc_total
            if sc_wr >= 0.7:
                adjustments.append(f"[recommend] Scanner {sc_name} producing {sc_wr*100:.0f}% winners — increase weight")

    # Conviction analysis
    high_wr = win_rate(perf["by_conviction"].get("HIGH", {}))
    spec_wr = win_rate(perf["by_conviction"].get("SPECULATIVE", {}))
    if high_wr and spec_wr:
        if high_wr > spec_wr + 0.2:
            adjustments.append("[recommend] HIGH conviction outperforming — prioritize HIGH setups")
        elif spec_wr > high_wr + 0.2:
            adjustments.append("[recommend] SPECULATIVE outperforming — conviction scoring needs recalibration")

    if adjustments:
        tagged = [f"[{now.strftime('%Y-%m-%d')}] {a}" for a in adjustments]
        data["adjustments"].extend(tagged)
        data["adjustments"] = data["adjustments"][-50:]

        existing_recs = []
        if os.path.exists(RECOMMENDATIONS_FILE):
            try:
                with open(RECOMMENDATIONS_FILE) as f:
                    existing_recs = json.load(f).get("recommendations", [])
            except Exception:
                pass
        new_recs = [a for a in tagged if "[recommend]" in a]
        _save_recommendations((existing_recs + new_recs)[-100:])

    data["last_weekly_report"] = now.isoformat()
    _save(data)

    total     = sum(s["wins"] + s["losses"] for s in perf["by_signal_type"].values())
    wins      = sum(s["wins"] for s in perf["by_signal_type"].values())
    losses    = sum(s["losses"] for s in perf["by_signal_type"].values())
    total_pnl = sum(float(s.get("total_pnl", 0)) for s in perf["by_signal_type"].values())
    avg_pnl   = total_pnl / total if total else 0

    from account import get_summary
    acct = get_summary()

    return {
        "total_trades":    total,
        "wins":            wins,
        "losses":          losses,
        "win_rate_pct":    round(wins / total * 100, 1) if total else 0,
        "avg_pnl_pct":     round(avg_pnl, 2),
        "current_balance": acct.get("balance", 0),
        "adjustments":     adjustments,
        "best_bucket":     max(bucket_rates, key=bucket_rates.get) if bucket_rates else "N/A",
        "passive_tracked": p_total,
    }


def get_best_time_buckets() -> list:
    data = _load()
    perf = data["performance"]

    def win_rate(d):
        total = d.get("wins", 0) + d.get("losses", 0)
        return d.get("wins", 0) / total if total >= 3 else None

    results = []
    for bucket, stats in perf.get("by_time_bucket", {}).items():
        wr = win_rate(stats)
        if wr is not None:
            results.append((bucket, wr))

    results.sort(key=lambda x: x[1], reverse=True)
    return results[:3]


def should_run_weekly() -> bool:
    data = _load()
    last = data.get("last_weekly_report", "")
    if not last:
        return True
    try:
        last_dt    = datetime.fromisoformat(last)
        days_since = (datetime.now() - last_dt.replace(tzinfo=None)).days
        return days_since >= 7
    except Exception:
        return True

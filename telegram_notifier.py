"""Telegram alert formatting and sending."""
import requests
import yfinance as yf
from datetime import datetime
from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TIMEZONE
from account import get_position_size, get_risk_amount


def _send(text: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"},
            timeout=10,
        )
    except Exception as e:
        print(f"[TG] Send error: {e}")


def send_morning_summary(regime: dict, top_sectors: list, news_events: list,
                         sector_rankings: list = None, best_windows: list = None) -> None:
    now = datetime.now(TIMEZONE)
    r        = regime.get("regime", "UNKNOWN")
    vix      = regime.get("vix", 0)
    spy      = regime.get("spy_price", 0)
    vs20     = regime.get("spy_vs_ema20", "?")
    vs50     = regime.get("spy_vs_ema50", "?")
    struct   = regime.get("market_structure", "?")

    if r == "BULL":
        regime_emoji = "🟢"
        trade_expect = "Long-only mode. Look for breakouts and accumulation setups."
        aggression   = "AGGRESSIVE — full position sizes"
    elif r == "BEAR":
        regime_emoji = "🔴"
        trade_expect = "Short mode active. Longs need extreme conviction. Shorts welcome."
        aggression   = "CAUTIOUS — prefer shorts, reduced sizes"
    elif r == "NEWS_DRIVEN":
        regime_emoji = "📰"
        trade_expect = "Major news driving markets. Focus on news-catalyst plays."
        aggression   = "SELECTIVE — news-driven setups only"
    else:
        regime_emoji = "🟡"
        trade_expect = "Mixed signals. Higher conviction required."
        aggression   = "REDUCED — smaller sizes"

    sectors_str = ", ".join(top_sectors[:4]) if top_sectors else "None identified"

    news_lines = []
    for ev in news_events[:3]:
        title = ev.get("title", "")[:80]
        news_lines.append(f"  • {title}")
    news_str = "\n".join(news_lines) if news_lines else "  No major news"

    sector_map_str = ""
    if sector_rankings:
        top3_str = " | ".join(f"{s['name']} ({s['pct_5d']:+.1f}%)" for s in sector_rankings[:3])
        bot3_str = " | ".join(f"{s['name']} ({s['pct_5d']:+.1f}%)" for s in sector_rankings[-3:])
        sector_map_str = (
            f"━━━━━━━━━━━━━━━━\n"
            f"*Sector Rotation:*\n"
            f"  🟢 Top: {top3_str}\n"
            f"  🔴 Weak: {bot3_str}\n"
        )

    windows_str = ""
    if best_windows:
        bw = " | ".join(f"{b[0]} ({b[1]*100:.0f}%WR)" for b in best_windows[:2])
        windows_str = f"*Best windows (hist):* {bw}\n"

    text = (
        f"{regime_emoji} *MORNING BRIEFING — {now.strftime('%A %b %d')}*\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"*Regime: {r}* | SPY ${spy:.2f} ({vs20} EMA20, {vs50} EMA50)\n"
        f"VIX {vix:.1f} | Structure: {struct}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"*Sectors today:* {sectors_str}\n"
        f"{sector_map_str}"
        f"━━━━━━━━━━━━━━━━\n"
        f"*Key News:*\n{news_str}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"{windows_str}"
        f"*Expect:* {trade_expect}\n"
        f"*Aggression:* {aggression}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"_Reply BOUGHT or IN to confirm any alert._"
    )
    _send(text)


def send_premarket_news_digest(futures: dict, global_mkts: dict,
                               news_events: list, pm_movers: list) -> None:
    now = datetime.now(TIMEZONE)

    fut_str = ""
    for name, pct in futures.items():
        emoji = "🟢" if pct >= 0 else "🔴"
        fut_str += f"  {emoji} {name}: {pct:+.2f}%\n"

    global_str = ""
    for name, pct in global_mkts.items():
        emoji = "🟢" if pct >= 0 else "🔴"
        global_str += f"  {emoji} {name}: {pct:+.2f}%\n"

    news_str = ""
    for ev in news_events[:4]:
        title = ev.get("title", "")[:80]
        news_str += f"  • {title}\n"

    movers_str = ""
    for m in pm_movers[:4]:
        sym  = m.get("symbol", "")
        pct  = m.get("gap_pct", 0)
        cat  = "w/ catalyst" if m.get("has_news") else "no catalyst"
        movers_str += f"  • *{sym}* {pct:+.1f}% {cat}\n"

    total_fut = sum(futures.values()) if futures else 0
    tone = "RISK-ON 🟢" if total_fut > 0.3 else ("RISK-OFF 🔴" if total_fut < -0.3 else "NEUTRAL 🟡")

    text = (
        f"🌅 *PRE-MARKET DIGEST — {now.strftime('%I:%M %p ET')}*\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"*US Futures:*\n{fut_str if fut_str else '  N/A'}"
        f"━━━━━━━━━━━━━━━━\n"
        f"*Global Markets:*\n{global_str if global_str else '  N/A'}"
        f"━━━━━━━━━━━━━━━━\n"
        f"*Overnight News:*\n{news_str if news_str else '  No major news'}"
        f"━━━━━━━━━━━━━━━━\n"
        f"*Pre-Market Movers:*\n{movers_str if movers_str else '  No major movers'}"
        f"━━━━━━━━━━━━━━━━\n"
        f"*Overall tone: {tone}*"
    )
    _send(text)


def send_trade_alert(result: dict) -> None:
    symbol     = result.get("symbol", "")
    signal     = result.get("signal", "")
    scanner    = result.get("scanner", "")
    trade_type = result.get("trade_type", "SWING TRADE")
    conviction = result.get("conviction", "SPECULATIVE")
    score      = result.get("score", 0)
    price      = result.get("price", 0)
    stop       = result.get("stop_loss", 0)
    target     = result.get("price_target", 0)
    rr         = result.get("rr_ratio", 0)
    reasons    = result.get("reasons", [])[:4]
    flags      = result.get("flags", [])
    intraday_pct = result.get("intraday_pct", 0)
    room_pct   = result.get("room_pct", 0)
    has_news   = result.get("has_news", False)
    news_hdl   = result.get("news_headline", "")
    sector     = result.get("sector", "")
    sector_rank = result.get("sector_rank", 0)
    insider    = result.get("insider_buying", False)
    insider_det = result.get("insider_detail", "")
    dark_pool  = result.get("dark_pool", False)
    dark_pool_det = result.get("dark_pool_detail", "")
    corr_warn  = result.get("correlation_warning", "")

    is_day   = trade_type == "DAY TRADE"
    is_short = signal in ("SHORT", "STRONG SELL", "SELL")
    is_pm    = scanner == "PREMARKET"
    is_ah    = scanner == "AFTERHOURS"

    is_awakening = scanner == "VOLUME_AWAKENING"
    is_open_drive = scanner in ("OPEN_DRIVE", "OPEN_SCAN")

    if is_awakening:
        header = f"🌋 *VOLUME AWAKENING: {symbol}*"
    elif is_open_drive:
        header = f"🚀 *OPEN DRIVE: {symbol}*"
    elif is_pm:
        header = f"🌅 *PRE-MARKET SETUP: {symbol}*"
    elif scanner == "DAY_NEWS" or (is_day and has_news):
        header = f"📰 *NEWS PLAY — DAY TRADE: {symbol}*"
    elif is_day:
        header = f"⚡ *BREAKOUT — DAY TRADE: {symbol}*"
    elif is_ah:
        header = f"🌙 *AFTER-HOURS: {symbol}*"
    elif is_short:
        header = f"🔻 *SHORT TRADE: {symbol}*"
    else:
        header = f"🚀 *SWING TRADE: {symbol}*"

    conv_label = "HIGH CONVICTION" if conviction == "HIGH" else "SPECULATIVE"
    conv_emoji = "🔥" if conviction == "HIGH" else "📊"

    now = datetime.now(TIMEZONE)
    market_minutes = (now.hour - 9) * 60 + now.minute - 30
    if is_pm:
        time_label = "PRE-MARKET — wait for open confirmation"
    elif is_ah:
        time_label = "AFTER-HOURS — watch pre-market continuation"
    elif market_minutes < 60:
        time_label = "EARLY — full position OK"
    elif market_minutes < 240:
        time_label = "MID SESSION — standard size"
    else:
        time_label = "LATE — smaller size or skip"

    pos_size    = min(get_position_size(conviction), 1500.0)
    shares      = int(pos_size / price) if price > 0 else 0
    dollar_buy  = round(shares * price, 2)
    stop_dist   = abs(price - stop)
    actual_risk = round(stop_dist * shares, 2) if shares > 0 else 0

    gain_pct = round((abs(target - price) / price * 100), 1) if price else 0
    stop_pct = round(abs(price - stop) / price * 100, 1) if price else 0

    if is_short:
        kill_note = f"Close above ${stop:.2f} — momentum reversal"
    elif is_day:
        kill_note = f"Break below ${stop:.2f} OR market tanks hard"
    elif is_pm:
        kill_note = f"Opens below ${price * 0.97:.2f} or gap fills back"
    else:
        bo_level  = float(result.get("bo_level", stop)) * 0.99
        kill_note = f"Close below ${bo_level:.2f} — breakout failed"

    reasons_str = "\n".join(f"  • {r}" for r in reasons)

    flags_all = list(flags)
    if insider:
        flags_all.append(f"INSIDER BUYING — {insider_det}")
    if dark_pool:
        flags_all.append(f"DARK POOL — {dark_pool_det}")
    flags_str = ""
    if flags_all:
        flags_str = "\n" + "\n".join(f"  ⚡ {f}" for f in flags_all)

    news_line = f"\n📰 *NEWS:* {news_hdl[:80]}" if has_news and news_hdl else ""
    sector_line = f"\n📊 Sector: {sector} (rank #{sector_rank}/11)" if sector and sector_rank else ""
    corr_line = f"\n⚠️ *{corr_warn}*" if corr_warn else ""

    text = (
        f"{header}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"{conv_emoji} {conv_label} | Score: {score}\n"
        f"⏰ {time_label}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"💵 Price: ${price:.2f}"
    )
    if intraday_pct:
        text += f" ({intraday_pct:+.1f}%)"
    text += "\n"
    text += (
        f"🎯 Target: ${target:.2f} (+{gain_pct:.1f}% potential)\n"
        f"🛑 Stop: ${stop:.2f} (-{stop_pct:.1f}%)\n"
        f"R/R: {rr:.1f} | Room: {room_pct:.0f}%\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"*Why it passed:*\n{reasons_str}"
        f"{news_line}"
        f"{sector_line}"
        f"{flags_str}"
        f"{corr_line}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"*Sizing:*\n"
        f"  Buy: ${dollar_buy:,.0f} (~{shares} shares) | Risk: ${actual_risk:.0f}\n"
        f"*Kills it:* {kill_note}\n"
    )

    if is_pm:
        text += "_PRE-MARKET: Wait for open. Enter at open if holding above gap._\n"
    elif is_ah:
        text += "_AFTER-HOURS: Watch pre-market tomorrow for follow-through._\n"
    elif is_day:
        text += "_DAY TRADE: EXIT BEFORE 4PM._\n"
    else:
        text += "_SWING: Hold 3-10 days. Manage with stop._\n"

    text += "\nReply *BOUGHT* or *IN AT [price]* to confirm."
    _send(text)


def send_eod_summary(regime: str, alerts_today: list, confirmed_trades_pnl: float,
                     account_balance: float) -> None:
    now = datetime.now(TIMEZONE)

    day_trades  = [a for a in alerts_today if a.get("signal_type", "") != "SWING TRADE"]
    swing_alerts = [a for a in alerts_today if a.get("signal_type", "") == "SWING TRADE"]

    perf_lines = []
    best_sym, best_pnl = "", -999
    worst_sym, worst_pnl = "", 999

    for alert in alerts_today:
        sym = alert.get("symbol", "")
        ap  = float(alert.get("alert_price", 0) or 0)
        pnl_val = 0.0
        pnl_eod = alert.get("pnl_eod_pct", "")
        try:
            if pnl_eod:
                pnl_val = float(pnl_eod)
            else:
                raise ValueError
        except (ValueError, TypeError):
            try:
                df_eod = yf.Ticker(sym).history(period="1d", interval="1m")
                if not df_eod.empty:
                    curr_p = float(df_eod["Close"].iloc[-1])
                    pnl_val = round((curr_p - ap) / ap * 100, 2) if ap > 0 else 0.0
            except Exception:
                pnl_val = 0.0

        emoji = "🟢" if pnl_val >= 0 else "🔴"
        confirmed = "✅" if alert.get("was_confirmed") == "yes" else "○"
        perf_lines.append(f"  {confirmed} {emoji} *{sym}*: alerted ${ap:.2f} → {pnl_val:+.1f}%")
        if pnl_val > best_pnl:
            best_pnl, best_sym = pnl_val, sym
        if pnl_val < worst_pnl:
            worst_pnl, worst_sym = pnl_val, sym

    perf_str = "\n".join(perf_lines[:10]) if perf_lines else "  No alerts today"
    pnl_emoji = "🟢" if confirmed_trades_pnl >= 0 else "🔴"

    text = (
        f"📊 *END OF DAY SUMMARY — {now.strftime('%A %b %d')}*\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"*Regime today:* {regime}\n"
        f"*Total alerts:* {len(alerts_today)} "
        f"({len(day_trades)} day trades, {len(swing_alerts)} swing)\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"*Alert performance:*\n{perf_str}\n"
    )

    if best_sym:
        text += f"🏆 *Best:* {best_sym} {best_pnl:+.1f}%\n"
    if worst_sym and worst_sym != best_sym:
        text += f"💔 *Worst:* {worst_sym} {worst_pnl:+.1f}%\n"

    text += (
        f"━━━━━━━━━━━━━━━━\n"
        f"{pnl_emoji} *Confirmed trades P&L today:* ${confirmed_trades_pnl:+.2f}\n"
        f"💰 *Account balance:* ${account_balance:,.2f}\n"
        f"_(✅ = you confirmed buying, ○ = tracked only)_"
    )
    _send(text)


def send_daily_limit_message(daily_pnl: float, balance: float) -> None:
    _send(
        f"🛑 *DAILY LOSS LIMIT REACHED*\n"
        f"Lost ${abs(daily_pnl):.2f} today — stopping all new alerts.\n"
        f"Account balance: ${balance:,.2f}\n"
        f"_Come back tomorrow. Protect the capital._"
    )


def send_exit_alert(symbol: str, reason: str, price: float,
                    entry: float, pnl_pct: float, urgent: bool = False) -> None:
    emoji     = "🛑" if urgent else "⚠️"
    pnl_emoji = "🟢" if pnl_pct >= 0 else "🔴"
    _send(
        f"{emoji} *{reason}: {symbol}*\n"
        f"Current: ${price:.2f} | Entry: ${entry:.2f}\n"
        f"{pnl_emoji} P&L: {pnl_pct:+.2f}%"
    )


def send_weekly_report(report: dict) -> None:
    total    = report.get("total_trades", 0)
    wins     = report.get("wins", 0)
    losses   = report.get("losses", 0)
    avg_pnl  = report.get("avg_pnl_pct", 0)
    win_rate = report.get("win_rate_pct", 0)
    balance  = report.get("current_balance", 0)
    best_bkt = report.get("best_bucket", "N/A")
    p_tracked = report.get("passive_tracked", 0)
    adjustments = report.get("adjustments", [])

    adj_str = "".join(f"  • {a}\n" for a in adjustments[:6])

    text = (
        f"📈 *WEEKLY PERFORMANCE REPORT*\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"Trades: {total} | Wins: {wins} | Losses: {losses}\n"
        f"Win rate: {win_rate:.0f}% | Avg P&L: {avg_pnl:+.1f}%\n"
        f"Balance: ${balance:,.2f}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"Best time window: {best_bkt}\n"
        f"Passive outcomes tracked: {p_tracked}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"*Adjustments made:*\n"
        f"{adj_str if adj_str else '  None this week'}"
    )
    _send(text)

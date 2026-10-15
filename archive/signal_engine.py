import pandas as pd
from config import (
    MAX_RSI_FOR_STRONG_BUY,
    STRONG_BUY_THRESHOLD, BUY_THRESHOLD,
    SELL_THRESHOLD, STRONG_SELL_THRESHOLD,
)
from patterns import detect_patterns


def get_signal(df, patterns=None):
    try:
        if patterns is None:
            patterns = detect_patterns(df)

        last       = df.iloc[-1]
        prev       = df.iloc[-2]
        close      = last["Close"]
        atr        = last["ATR"]
        support    = last["Support"]
        resistance = last["Resistance"]
        rsi        = last["RSI"]
        rsi_prev   = prev["RSI"]

        score   = 0
        reasons = []

        hard_cap = 40 if rsi > MAX_RSI_FOR_STRONG_BUY else 100

        above_ema50   = close > last["EMA50"]
        above_ema200  = close > last["EMA200"]
        ema_uptrend   = last["EMA20"] > last["EMA50"] > last["EMA200"]
        ema_downtrend = last["EMA20"] < last["EMA50"] < last["EMA200"]

        if ema_uptrend and above_ema200:
            score += 15
            reasons.append("Uptrend confirmed (EMA20 > 50 > 200)")
        elif ema_downtrend:
            score -= 15
            reasons.append("Downtrend confirmed (EMA20 < 50 < 200)")
        elif above_ema200:
            score += 5
            reasons.append("Above EMA200 — long-term bullish")
        else:
            score -= 10
            reasons.append("Below EMA200 — long-term bearish")

        dist_ema20     = (close - last["EMA20"]) / last["EMA20"]
        dist_ema50     = (close - last["EMA50"]) / last["EMA50"]
        near_ema20     = abs(dist_ema20) < 0.03
        near_ema50     = abs(dist_ema50) < 0.04
        extended_above = dist_ema20 > 0.08

        if near_ema20 and above_ema50:
            score += 20
            reasons.append("Pulled back to EMA20 in uptrend — ideal entry")
        elif near_ema50 and above_ema200:
            score += 15
            reasons.append("Pulled back to EMA50 — strong entry zone")
        elif extended_above:
            score -= 20
            reasons.append("Price extended above EMA20 — do not chase")
        elif dist_ema20 > 0.04:
            score -= 8
            reasons.append("Price above EMA20 — entry not ideal")

        rsi_rising  = rsi > rsi_prev
        rsi_was_low = rsi_prev < 50
        rsi_reset   = rsi_prev < 45 and rsi > rsi_prev

        if rsi_reset:
            score += 20
            reasons.append(f"RSI reset and turning up ({rsi_prev:.1f} -> {rsi:.1f})")
        elif rsi_was_low and rsi_rising and 45 <= rsi <= 60:
            score += 15
            reasons.append(f"RSI rising from low base ({rsi:.1f})")
        elif 50 < rsi <= 60 and rsi_rising:
            score += 8
            reasons.append(f"RSI bullish momentum ({rsi:.1f})")
        elif 60 < rsi <= 70:
            score -= 5
            reasons.append(f"RSI getting extended ({rsi:.1f})")
        elif rsi > 70:
            score -= 15
            reasons.append(f"RSI overbought ({rsi:.1f}) — do not buy")
        elif rsi < 30:
            score += 8
            reasons.append(f"RSI oversold ({rsi:.1f}) — potential reversal")
        elif rsi < 45:
            score -= 5
            reasons.append(f"RSI weak ({rsi:.1f})")

        macd_just_crossed_up   = last["MACD"] > last["MACD_signal"] and prev["MACD"] <= prev["MACD_signal"]
        macd_just_crossed_down = last["MACD"] < last["MACD_signal"] and prev["MACD"] >= prev["MACD_signal"]
        macd_bullish           = last["MACD"] > last["MACD_signal"]
        macd_hist_growing      = last["MACD_hist"] > prev["MACD_hist"]

        if macd_just_crossed_up:
            score += 18
            reasons.append("MACD fresh bullish cross")
        elif macd_bullish and macd_hist_growing:
            score += 8
            reasons.append("MACD bullish and strengthening")
        elif macd_just_crossed_down:
            score -= 18
            reasons.append("MACD fresh bearish cross")
        elif not macd_bullish:
            score -= 8
            reasons.append("MACD bearish")

        bb_pct = last["BB_pct"]
        if bb_pct < 0.2:
            score += 10
            reasons.append("Price near lower BB — oversold")
        elif bb_pct < 0.35:
            score += 5
            reasons.append("Price in lower half of BB — good entry zone")
        elif bb_pct > 0.9:
            score -= 12
            reasons.append("Price at top of BB — overbought")
        elif bb_pct > 0.75:
            score -= 6
            reasons.append("Price near upper BB — extended")

        if close > last["VWAP"] * 1.005:
            score += 5
            reasons.append("Above VWAP")
        elif close < last["VWAP"]:
            score -= 5
            reasons.append("Below VWAP")

        if last["Vol_ratio"] > 1.5:
            score += 10
            reasons.append(f"Volume spike on signal ({last['Vol_ratio']:.1f}x)")
        elif last["Vol_ratio"] > 1.0:
            score += 4
            reasons.append(f"Above average volume ({last['Vol_ratio']:.1f}x)")
        elif last["Vol_ratio"] < 0.6:
            score -= 10
            reasons.append("Weak volume — do not trust move")
        elif last["Vol_ratio"] < 0.8:
            score -= 4
            reasons.append("Below average volume")

        adx = last["ADX"]
        if adx > 30:
            score += 8
            reasons.append(f"Strong trend (ADX {adx:.1f})")
        elif adx > 20:
            score += 3
            reasons.append(f"Moderate trend (ADX {adx:.1f})")
        elif adx < 15:
            score -= 10
            reasons.append(f"Trendless market (ADX {adx:.1f}) — skip")

        if last["HH"] and last["HL"]:
            score += 8
            reasons.append("Higher highs and higher lows")
        elif last["LH"] and last["LL"]:
            score -= 8
            reasons.append("Lower highs and lower lows")

        stoch_oversold_cross = last["Stoch_K"] < 30 and last["Stoch_K"] > last["Stoch_D"]
        stoch_overbought     = last["Stoch_K"] > 80

        if stoch_oversold_cross:
            score += 12
            reasons.append("Stochastic oversold with bullish cross")
        elif stoch_overbought and last["Stoch_K"] < last["Stoch_D"]:
            score -= 10
            reasons.append("Stochastic overbought bearish cross")
        elif stoch_overbought:
            score -= 5
            reasons.append("Stochastic overbought")

        obv_series = pd.Series(df["OBV"].values)
        if obv_series.iloc[-1] > obv_series.iloc[-5] * 1.02:
            score += 6
            reasons.append("OBV rising — accumulation")
        elif obv_series.iloc[-1] < obv_series.iloc[-5]:
            score -= 6
            reasons.append("OBV falling — distribution")

        mom5 = last["Mom_5"]
        if 1 < mom5 <= 5:
            score += 8
            reasons.append(f"Healthy momentum (+{mom5:.1f}%)")
        elif mom5 > 8:
            score -= 10
            reasons.append(f"Momentum too extended (+{mom5:.1f}%) — chasing")
        elif mom5 < -8:
            score -= 8
            reasons.append(f"Strong negative momentum ({mom5:.1f}%)")
        elif mom5 < -3:
            score -= 3
            reasons.append(f"Negative momentum ({mom5:.1f}%)")

        if patterns.get("bullish_engulfing"):
            score += 12
            reasons.append("Bullish engulfing")
        if patterns.get("bearish_engulfing"):
            score -= 12
            reasons.append("Bearish engulfing")
        if patterns.get("hammer"):
            score += 10
            reasons.append("Hammer — bullish reversal")
        if patterns.get("shooting_star"):
            score -= 10
            reasons.append("Shooting star — bearish reversal")
        if patterns.get("morning_star"):
            score += 12
            reasons.append("Morning star")
        if patterns.get("evening_star"):
            score -= 12
            reasons.append("Evening star")
        if patterns.get("bullish_marubozu"):
            score += 6
            reasons.append("Bullish marubozu")
        if patterns.get("doji"):
            score -= 3
            reasons.append("Doji — indecision")

        if patterns.get("bull_flag"):
            score += 18
            reasons.append("Bull flag — high probability setup")
        if patterns.get("bear_flag"):
            score -= 18
            reasons.append("Bear flag")
        if patterns.get("ascending_triangle"):
            score += 12
            reasons.append("Ascending triangle")
        if patterns.get("descending_triangle"):
            score -= 12
            reasons.append("Descending triangle")
        if patterns.get("falling_wedge"):
            score += 12
            reasons.append("Falling wedge — bullish reversal")
        if patterns.get("rising_wedge"):
            score -= 12
            reasons.append("Rising wedge — bearish reversal")
        if patterns.get("tight_consolidation"):
            score += 10
            reasons.append("Tight consolidation — breakout incoming")

        if patterns.get("breakout_up"):
            score += 20
            reasons.append("Breakout above resistance with volume")
        if patterns.get("breakdown"):
            score -= 20
            reasons.append("Breakdown below support")
        if patterns.get("fakeout"):
            score -= 15
            reasons.append("Fakeout — rejected at resistance")

        if patterns.get("near_demand"):
            score += 8
            reasons.append("Near demand zone")
        if patterns.get("near_supply"):
            score -= 8
            reasons.append("Near supply zone")

        if patterns.get("volume_dryup"):
            score += 5
            reasons.append("Volume dry up — coiling")
        if patterns.get("volume_climax"):
            score -= 10
            reasons.append("Volume climax — potential reversal")

        phase = patterns.get("market_phase", "transition")
        if phase == "markup":
            score += 6
            reasons.append("Market phase: Markup")
        elif phase == "accumulation":
            score += 8
            reasons.append("Market phase: Accumulation")
        elif phase == "markdown":
            score -= 8
            reasons.append("Market phase: Markdown")
        elif phase == "distribution":
            score -= 6
            reasons.append("Market phase: Distribution")

        if last["Gap_up"] and last["Vol_ratio"] > 1.5:
            score += 8
            reasons.append(f"Gap up with volume (+{last['Gap_pct']:.1f}%)")
        elif last["Gap_down"]:
            score -= 6
            reasons.append(f"Gap down ({last['Gap_pct']:.1f}%)")

        for fib_level, label in [("Fib_618", "61.8%"), ("Fib_500", "50%"), ("Fib_382", "38.2%")]:
            fib = last[fib_level]
            if abs(close - fib) / close < 0.015:
                score += 6
                reasons.append(f"Bouncing off Fibonacci {label}")
                break

        score = max(-100, min(hard_cap, score))

        if score >= STRONG_BUY_THRESHOLD:
            signal = "STRONG BUY"
        elif score >= BUY_THRESHOLD:
            signal = "BUY"
        elif score <= STRONG_SELL_THRESHOLD:
            signal = "STRONG SELL"
        elif score <= SELL_THRESHOLD:
            signal = "SELL"
        else:
            signal = "HOLD"

        stop_loss    = round(close - (atr * 1.5), 2)
        price_target = round(close + (atr * 4.5), 2)
        risk         = close - stop_loss
        reward       = price_target - close
        rr_ratio     = round(reward / risk, 2) if risk > 0 else 0

        return {
            "signal":       signal,
            "score":        score,
            "confidence":   abs(score),
            "price":        round(close, 2),
            "price_target": price_target,
            "stop_loss":    stop_loss,
            "support":      round(support, 2),
            "resistance":   round(resistance, 2),
            "rr_ratio":     rr_ratio,
            "rsi":          round(rsi, 1),
            "atr":          round(atr, 2),
            "phase":        phase,
            "reasons":      reasons,
        }

    except Exception as e:
        return {"signal": "ERROR", "score": 0, "confidence": 0, "reasons": [str(e)]}

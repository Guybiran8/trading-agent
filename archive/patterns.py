def detect_patterns(df):
    patterns = {}

    close  = df["Close"]
    open_  = df["Open"]
    high   = df["High"]
    low    = df["Low"]
    volume = df["Volume"]

    c  = close.iloc[-1]
    c1 = close.iloc[-2]
    c2 = close.iloc[-3]
    o  = open_.iloc[-1]
    o1 = open_.iloc[-2]
    h  = high.iloc[-1]
    h1 = high.iloc[-2]
    l  = low.iloc[-1]
    l1 = low.iloc[-2]

    body         = abs(c - o)
    body1        = abs(c1 - o1)
    candle_range = h - l

    patterns["doji"]         = body < (candle_range * 0.1) and candle_range > 0

    lower_wick = min(o, c) - l
    upper_wick = h - max(o, c)
    patterns["hammer"]        = (lower_wick > body * 2) and (upper_wick < body * 0.5) and (c > o)
    patterns["shooting_star"] = (upper_wick > body * 2) and (lower_wick < body * 0.5) and (c < o)

    patterns["bullish_engulfing"] = (c1 < o1) and (c > o) and (c > o1) and (o < c1)
    patterns["bearish_engulfing"] = (c1 > o1) and (c < o) and (c < o1) and (o > c1)

    patterns["morning_star"] = (c2 < open_.iloc[-3]) and (body1 < body * 0.3) and (c > c2)
    patterns["evening_star"] = (c2 > open_.iloc[-3]) and (body1 < body * 0.3) and (c < c2)

    patterns["bullish_marubozu"] = (c > o) and (body > candle_range * 0.9)
    patterns["bearish_marubozu"] = (c < o) and (body > candle_range * 0.9)

    recent_high   = high.iloc[-10:-1].max()
    recent_low    = low.iloc[-10:-1].min()
    prior_move    = close.iloc[-10] - close.iloc[-20]
    prior_drop    = close.iloc[-20] - close.iloc[-10]
    consolidation = (recent_high - recent_low) / recent_high
    patterns["bull_flag"] = (prior_move > 0) and (consolidation < 0.05) and (c > df["EMA20"].iloc[-1])
    patterns["bear_flag"] = (prior_drop > 0) and (consolidation < 0.05) and (c < df["EMA20"].iloc[-1])

    highs_flat    = high.iloc[-10:].std() / high.iloc[-10:].mean() < 0.02
    lows_rising   = low.iloc[-10:].is_monotonic_increasing
    lows_flat     = low.iloc[-10:].std() / low.iloc[-10:].mean() < 0.02
    highs_falling = high.iloc[-10:].is_monotonic_decreasing
    patterns["ascending_triangle"]  = highs_flat and lows_rising
    patterns["descending_triangle"] = lows_flat and highs_falling

    high_slope = (high.iloc[-1] - high.iloc[-10]) / 10
    low_slope  = (low.iloc[-1]  - low.iloc[-10])  / 10
    patterns["rising_wedge"]  = (high_slope > 0) and (low_slope > high_slope)
    patterns["falling_wedge"] = (high_slope < 0) and (low_slope < high_slope)

    resistance_20 = high.iloc[-21:-1].max()
    support_20    = low.iloc[-21:-1].min()
    avg_volume    = volume.iloc[-20:].mean()
    last_volume   = volume.iloc[-1]
    patterns["breakout_up"] = (c > resistance_20) and (last_volume > avg_volume * 1.5)
    patterns["breakdown"]   = (c < support_20)    and (last_volume > avg_volume * 1.5)
    patterns["fakeout"]     = (h > resistance_20) and (c < resistance_20) and (body > candle_range * 0.5)

    bb_width = df["BB_width"].iloc[-1]
    patterns["tight_consolidation"] = bb_width < df["BB_width"].rolling(50).mean().iloc[-1] * 0.5

    patterns["near_supply"] = (c > resistance_20 * 0.97) and (c < resistance_20 * 1.01)
    patterns["near_demand"] = (c > support_20 * 0.99)    and (c < support_20 * 1.03)

    patterns["volume_climax"] = last_volume > avg_volume * 3
    patterns["volume_dryup"]  = last_volume < avg_volume * 0.5

    ema20  = df["EMA20"].iloc[-1]
    ema50  = df["EMA50"].iloc[-1]
    ema200 = df["EMA200"].iloc[-1]
    adx    = df["ADX"].iloc[-1]

    if c > ema20 > ema50 > ema200 and adx > 25:
        patterns["market_phase"] = "markup"
    elif c < ema20 < ema50 < ema200 and adx > 25:
        patterns["market_phase"] = "markdown"
    elif adx < 20 and c > ema200:
        patterns["market_phase"] = "accumulation"
    elif adx < 20 and c < ema200:
        patterns["market_phase"] = "distribution"
    else:
        patterns["market_phase"] = "transition"

    return patterns

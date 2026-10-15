from data_fetcher import get_stock_data
from indicators import add_indicators


def get_market_context():
    try:
        context = {}

        for ticker, name in [("SPY", "SP500"), ("QQQ", "NASDAQ")]:
            df = get_stock_data(ticker, period="3mo")
            if df is None:
                continue
            df = add_indicators(df)

            last  = df.iloc[-1]
            close = last["Close"]

            if close > last["EMA20"] > last["EMA50"]:
                trend = "bullish"
            elif close < last["EMA20"] < last["EMA50"]:
                trend = "bearish"
            else:
                trend = "neutral"

            rsi  = last["RSI"]
            macd = last["MACD"] > last["MACD_signal"]

            perf_5d  = round(df["Close"].pct_change(5).iloc[-1]  * 100, 2)
            perf_20d = round(df["Close"].pct_change(20).iloc[-1] * 100, 2)

            context[name] = {
                "price":    round(close, 2),
                "trend":    trend,
                "rsi":      round(rsi, 1),
                "macd_bull": macd,
                "perf_5d":  perf_5d,
                "perf_20d": perf_20d,
                "above_200": close > last["EMA200"],
                "vwap_bull": close > last["VWAP"],
            }

        market_score = 0
        for name in ["SP500", "NASDAQ"]:
            if name not in context:
                continue
            if context[name]["trend"] == "bullish":
                market_score += 2
            elif context[name]["trend"] == "bearish":
                market_score -= 2
            if context[name]["macd_bull"]:
                market_score += 1
            if context[name]["above_200"]:
                market_score += 1
            if context[name]["perf_5d"] > 0:
                market_score += 1
            else:
                market_score -= 1

        if market_score >= 6:
            market_condition = "STRONG BULL"
        elif market_score >= 2:
            market_condition = "MILD BULL"
        elif market_score <= -6:
            market_condition = "STRONG BEAR"
        elif market_score <= -2:
            market_condition = "MILD BEAR"
        else:
            market_condition = "NEUTRAL"

        context["condition"] = market_condition
        context["score"]     = market_score

        return context

    except Exception as e:
        return {"condition": "UNKNOWN", "score": 0}

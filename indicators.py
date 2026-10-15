import pandas as pd
import numpy as np


def add_indicators(df):
    try:
        close  = df["Close"].astype(float)
        high   = df["High"].astype(float)
        low    = df["Low"].astype(float)
        volume = df["Volume"].astype(float)
        open_  = df["Open"].astype(float)

        df["EMA9"]   = close.ewm(span=9,   adjust=False).mean()
        df["EMA20"]  = close.ewm(span=20,  adjust=False).mean()
        df["EMA50"]  = close.ewm(span=50,  adjust=False).mean()
        df["EMA150"] = close.ewm(span=150, adjust=False).mean()
        df["EMA200"] = close.ewm(span=200, adjust=False).mean()
        df["SMA20"]  = close.rolling(20).mean()
        df["SMA50"]  = close.rolling(50).mean()

        delta = close.diff()
        gain  = delta.clip(lower=0).rolling(14).mean()
        loss  = (-delta.clip(upper=0)).rolling(14).mean()
        rs    = gain / loss
        df["RSI"] = 100 - (100 / (1 + rs))

        ema12             = close.ewm(span=12, adjust=False).mean()
        ema26             = close.ewm(span=26, adjust=False).mean()
        df["MACD"]        = ema12 - ema26
        df["MACD_signal"] = df["MACD"].ewm(span=9, adjust=False).mean()
        df["MACD_hist"]   = df["MACD"] - df["MACD_signal"]

        df["BB_mid"]   = close.rolling(20).mean()
        bb_std         = close.rolling(20).std()
        df["BB_upper"] = df["BB_mid"] + 2 * bb_std
        df["BB_lower"] = df["BB_mid"] - 2 * bb_std
        df["BB_width"] = (df["BB_upper"] - df["BB_lower"]) / df["BB_mid"]
        df["BB_pct"]   = (close - df["BB_lower"]) / (df["BB_upper"] - df["BB_lower"])

        tr = pd.concat([
            high - low,
            (high - close.shift()).abs(),
            (low  - close.shift()).abs(),
        ], axis=1).max(axis=1)
        df["ATR"] = tr.rolling(14).mean()

        typical        = (high + low + close) / 3
        df["VWAP"]     = (typical * volume).cumsum() / volume.cumsum()

        df["Vol_MA20"]  = volume.rolling(20).mean()
        df["Vol_ratio"] = volume / df["Vol_MA20"].replace(0, np.nan)
        df["Vol_spike"] = df["Vol_ratio"] > 2.0

        obv = [0]
        for i in range(1, len(df)):
            if close.iloc[i] > close.iloc[i - 1]:
                obv.append(obv[-1] + volume.iloc[i])
            elif close.iloc[i] < close.iloc[i - 1]:
                obv.append(obv[-1] - volume.iloc[i])
            else:
                obv.append(obv[-1])
        df["OBV"]    = obv
        df["OBV_MA"] = pd.Series(obv, index=df.index).rolling(20).mean()

        plus_dm  = high.diff().clip(lower=0)
        minus_dm = (-low.diff()).clip(lower=0)
        mask_p   = plus_dm < minus_dm
        mask_m   = minus_dm < plus_dm
        plus_dm[mask_p]  = 0
        minus_dm[mask_m] = 0
        atr14    = tr.rolling(14).mean()
        plus_di  = 100 * (plus_dm.rolling(14).mean()  / atr14)
        minus_di = 100 * (minus_dm.rolling(14).mean() / atr14)
        dx       = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
        df["ADX"]      = dx.rolling(14).mean()
        df["Plus_DI"]  = plus_di
        df["Minus_DI"] = minus_di

        low14     = low.rolling(14).min()
        high14    = high.rolling(14).max()
        rng14     = (high14 - low14).replace(0, np.nan)
        df["Stoch_K"] = 100 * (close - low14) / rng14
        df["Stoch_D"] = df["Stoch_K"].rolling(3).mean()

        df["Resistance"] = high.rolling(20).max()
        df["Support"]    = low.rolling(20).min()

        df["HH"] = (high > high.shift(1)) & (high.shift(1) > high.shift(2))
        df["HL"] = (low  > low.shift(1))  & (low.shift(1)  > low.shift(2))
        df["LH"] = (high < high.shift(1)) & (high.shift(1) < high.shift(2))
        df["LL"] = (low  < low.shift(1))  & (low.shift(1)  < low.shift(2))

        df["Gap_up"]   = open_ > close.shift(1) * 1.01
        df["Gap_down"] = open_ < close.shift(1) * 0.99
        df["Gap_pct"]  = (open_ - close.shift(1)) / close.shift(1) * 100

        df["Mom_5"]  = close.pct_change(5)  * 100
        df["Mom_10"] = close.pct_change(10) * 100
        df["Mom_20"] = close.pct_change(20) * 100

        df.dropna(inplace=True)
        return df
    except Exception as e:
        print(f"[Indicators] Error: {e}")
        return df

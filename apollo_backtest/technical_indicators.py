class SupertrendIndicator:
    def __init__(self, period=10, multiplier=3.0):
        self.period = period
        self.multiplier = multiplier

    def calculate(self, df):
        df = df.copy()

        # ATR using Wilder's smoothing
        df['H-L'] = df['High'] - df['Low']
        df['H-PC'] = abs(df['High'] - df['Close'].shift(1))
        df['L-PC'] = abs(df['Low'] - df['Close'].shift(1))
        df['TR'] = df[['H-L', 'H-PC', 'L-PC']].max(axis=1)
        df['ATR'] = df['TR'].ewm(alpha=1/self.period, adjust=False).mean()

        hl2 = (df['High'] + df['Low']) / 2
        df['UpperBand'] = hl2 + self.multiplier * df['ATR']
        df['LowerBand'] = hl2 - self.multiplier * df['ATR']

        trend = True
        supertrend = []

        for i in range(len(df)):
            if i < self.period:
                supertrend.append(None)
                continue

            curr_close = df['Close'].iloc[i]
            prev_upper = df['UpperBand'].iloc[i - 1]
            prev_lower = df['LowerBand'].iloc[i - 1]

            if curr_close > prev_upper:
                trend = True
            elif curr_close < prev_lower:
                trend = False
            else:
                # maintain previous trend, tighten band
                if trend and df['LowerBand'].iloc[i] < prev_lower:
                    df.loc[df.index[i], 'LowerBand'] = prev_lower
                if not trend and df['UpperBand'].iloc[i] > prev_upper:
                    df.loc[df.index[i], 'UpperBand'] = prev_upper

            band_value = df['LowerBand'].iloc[i] if trend else df['UpperBand'].iloc[i]
            supertrend.append(band_value)

        df['Supertrend'] = supertrend

        return df.drop(columns=['H-L', 'H-PC', 'L-PC', 'TR', 'ATR', 'UpperBand', 'LowerBand'])

class EMAIndicator:
    def __init__(self, period=20, column='Close'):
        self.period = period
        self.column = column

    def calculate(self, df):
        df = df.copy()
        df[f'EMA_{self.period}'] = df[self.column].ewm(span=self.period, adjust=False).mean()
        return df


class SignalGenerator:
    def __init__(self, ema_period=20):
        self.ema_col = f'EMA_{ema_period}'

    def generate(self, df):
        df = df.copy()
        df['Signal'] = "None"

        for i in range(1, len(df)):
            prev_close = df['Close'].iloc[i - 1]
            curr_close = df['Close'].iloc[i]
            prev_st = df['Supertrend'].iloc[i - 1]
            curr_st = df['Supertrend'].iloc[i]

            above_ema = df['Close'].iloc[i] > df[self.ema_col].iloc[i]
            below_ema = df['Close'].iloc[i] < df[self.ema_col].iloc[i]

            # Bullish crossover
            if (prev_close < prev_st) and (curr_close > curr_st) and above_ema:
                df.loc[df.index[i], 'Signal'] = "Buy"
            # Bearish crossover
            elif (prev_close > prev_st) and (curr_close < curr_st) and below_ema:
                df.loc[df.index[i], 'Signal'] = "Sell"

        return df
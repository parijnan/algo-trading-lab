"""Camarilla pivot levels from the previous session's high, low and close. Pure arithmetic, no data access."""

import janus_configs as configs


def camarilla_levels(high: float, low: float, close: float) -> dict:
    """R1..R7, S1..S7 plus 'C' (previous close) and 'R' (previous range). Levels R1-R4/S1-S4 are C +/- R*FACTOR/divisor; R5 = (H/L)*C and
    S5 is its mirror about C; R6 = R5 + 1.168 * (R5 - R4) S6 mirrors it, R7 = R6 + 1.168 * (R6 - R5) and S7 mirrors it."""
    if not (high >= low > 0):
        raise ValueError(f'bad previous-session range: high={high} low={low}')
    rng = high - low
    out = {'C': close, 'R': rng}
    for k, div in configs.LEVEL_DIVISORS.items():
        step = rng * configs.CAMARILLA_FACTOR / div
        out[f'R{k}'] = close + step
        out[f'S{k}'] = close - step
    r5 = (high / low) * close
    out['R5'] = r5
    out['S5'] = close - (r5 - close)
    r6 = r5 + configs.R6_FACTOR * (r5 - out['R4'])
    out['R6'] = r6
    out['S6'] = close - (r6 - close)
    r7 = r6 + configs.R7_FACTOR * (r6 - r5)
    out['R7'] = r7
    out['S7'] = close - (r7 - close)
    return out


def range_states(days: list) -> list:
    """Setter-candle range after each session of ONE contract. `days` is oldest first, each a dict with date, high, low, close. The first session is the first setter.
    After a session, a close strictly above the range high or strictly below the range low (judged against the range as it stood BEFORE that session) starts a new
    range from that session's own High and Low; otherwise the range widens to include the session's High and Low. Returns one dict per session: date, high, low,
    setter_date, age (sessions since the setter, the setter itself being 1) and extensions (wick extensions since the setter)."""
    out = []
    rh = rl = setter = None
    age = ext = 0
    for d in days:
        if rh is None or d['close'] > rh or d['close'] < rl:
            rh, rl, setter, age, ext = d['high'], d['low'], d['date'], 1, 0
        else:
            if d['high'] > rh or d['low'] < rl:
                ext += 1
            rh, rl = max(rh, d['high']), min(rl, d['low'])
            age += 1
        out.append({'date': d['date'], 'high': rh, 'low': rl, 'setter_date': setter, 'age': age, 'extensions': ext})
    return out

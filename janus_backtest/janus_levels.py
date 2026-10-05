"""Camarilla pivot levels from the previous session's high, low and close. Pure arithmetic, no data access."""

import janus_configs as configs


def camarilla_levels(high: float, low: float, close: float) -> dict:
    """R1..R6, S1..S6 plus 'C' (previous close) and 'R' (previous range). Levels R1-R4/S1-S4 are C +/- R*FACTOR/divisor; R5 = (H/L)*C and
    S5 is its mirror about C; R6 = R5 + 1.168 * (R5 - R4) and S6 mirrors it."""
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
    return out

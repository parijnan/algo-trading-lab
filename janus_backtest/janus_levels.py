"""Camarilla pivot levels from the previous session's high, low and close. Pure arithmetic, no data access."""

import janus_configs as configs


def camarilla_levels(high: float, low: float, close: float) -> dict:
    """R1..R5, S1..S5 plus 'C' (previous close) and 'R' (previous range). Levels R1-R4/S1-S4 are C +/- R*FACTOR/divisor; R5 = (H/L)*C and
    S5 is its mirror about C."""
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
    return out

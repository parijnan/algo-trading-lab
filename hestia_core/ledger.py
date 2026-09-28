"""Position arithmetic shared by the core's per-engine ledger and the simulated broker's account book."""

from typing import Optional, Tuple


def apply_fill(net: int, avg: Optional[float], signed_lots: int, price: float) -> Tuple[int, Optional[float]]:
    """New (net_lots, avg_price) after `signed_lots` fill at `price`. Adding to a position averages in, reducing keeps the
    average, flattening clears it, and crossing through zero restarts the average at the fill price."""
    new = net + signed_lots
    if net == 0 or (net > 0) == (signed_lots > 0):
        return new, (price if net == 0 else (abs(net) * avg + abs(signed_lots) * price) / abs(new))
    if new == 0:
        return 0, None
    if (new > 0) != (net > 0):
        return new, price
    return new, avg

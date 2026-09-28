"""
The live tick feed as the data service sees it, and an adapter over the repo's shared `websocket_feed.SharedFeed`.

FeedPort is what LiveData needs: subscribe or unsubscribe a token, the last price, the tick-aggregated OHLC since the last
read (which RESETS on read, so exactly one consumer per token: LiveData is that consumer), and the age of the last tick.
"""

from __future__ import annotations

from typing import Dict, Iterable, Optional, Protocol, runtime_checkable


@runtime_checkable
class FeedPort(Protocol):
    def subscribe(self, tokens: Iterable[str]) -> None: ...
    def unsubscribe(self, tokens: Iterable[str]) -> None: ...
    def get_ltp(self, token: str) -> Optional[float]: ...
    def get_ohlc(self, token: str) -> Optional[Dict[str, float]]: ...          # resets the window on every read
    def last_tick_age(self, token: str) -> Optional[float]: ...                # seconds; None if no tick has ever arrived


class SharedFeedAdapter:
    """FeedPort over websocket_feed.SharedFeed for one exchange type (Prometheus passes MCX_FO_WS_EXCHANGE_TYPE)."""

    def __init__(self, feed, exchange_type: int):
        self._feed, self._exchange_type = feed, exchange_type

    def subscribe(self, tokens):
        self._feed.subscribe_options(list(tokens), exchange_type=self._exchange_type)

    def unsubscribe(self, tokens):
        self._feed.unsubscribe_options(list(tokens), exchange_type=self._exchange_type)

    def get_ltp(self, token):
        return self._feed.get_ltp(token)

    def get_ohlc(self, token):
        return self._feed.get_ohlc(token)

    def last_tick_age(self, token):
        return self._feed.get_last_tick_age(token)

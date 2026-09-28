"""Executors for the blocking half of a live adapter. The live Hestia uses a ThreadPoolExecutor; tests and single-threaded
replays use InlineExecutor, which runs the work at once on the caller's thread (so a scripted double needs no threads)."""

from concurrent.futures import Future


class InlineExecutor:
    def submit(self, fn, *args, **kwargs) -> Future:
        fut: Future = Future()
        try:
            fut.set_result(fn(*args, **kwargs))
        except BaseException as exc:            # noqa: BLE001 - mirror ThreadPoolExecutor: errors surface on the future
            fut.set_exception(exc)
        return fut

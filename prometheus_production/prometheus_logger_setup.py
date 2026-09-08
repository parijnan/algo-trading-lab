import logging
from datetime import date
from prometheus_configs import LOG_DIR


def get_logger(name: str) -> logging.Logger:
    """
    Named-logger pattern (matches iris_logger_setup.py) — attaches handlers
    directly to this logger rather than calling logging.basicConfig() on the
    root logger. This sidesteps the "whoever calls basicConfig() first wins"
    bug hit and fixed in mcx_live_downloader.py: prometheus_functions.py
    imports data_downloader_mcx (for fetch_candle_chunk/date_range_chunks,
    §1), which claims the root logger with its own basicConfig() at import
    time — a named logger's own handlers are unaffected by that either way.
    """
    LOG_DIR.mkdir(exist_ok=True)
    log_file = LOG_DIR / f'prometheus_{date.today().strftime("%Y%m%d")}.log'

    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)
    # 2026-09-08: prometheus_functions.py imports data_downloader_mcx, whose
    # own module-level logging.basicConfig() attaches a StreamHandler to the
    # ROOT logger (different format: "%(asctime)s [%(levelname)s] %(message)s").
    # Without this, every record logged here ALSO propagates to that root
    # handler, so anything redirected to stdout (cron's/a manual restart's
    # log file) gets each line twice, in two different formats. This logger's
    # own two handlers (below) are unaffected either way.
    logger.propagate = False

    fh = logging.FileHandler(log_file)
    fh.setLevel(logging.DEBUG)

    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)

    fmt = logging.Formatter('%(asctime)s  %(levelname)-8s  %(name)s  %(message)s',
                            datefmt='%Y-%m-%d %H:%M:%S')
    fh.setFormatter(fmt)
    ch.setFormatter(fmt)

    logger.addHandler(fh)
    logger.addHandler(ch)
    return logger

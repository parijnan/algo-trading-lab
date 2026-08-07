"""
logger_setup.py — Artemis Production Logging Setup
Configures a logger that writes to both console and logs/debug.log.

Import and call get_logger(__name__) in each module:
    from logger_setup import get_logger
    logger = get_logger(__name__)

Log level is controlled by LOG_LEVEL in configs.py.
    DEBUG — all variable values, every candle close, every LTP poll
    INFO  — startup, entries, exits, errors only (production setting)
"""

import os
import logging
from artemis_configs import LOG_LEVEL, LOGS_DIR


def get_logger(name: str) -> logging.Logger:
    """
    Return a logger configured with console and file handlers.
    Multiple calls with the same name return the same logger instance
    (Python logging module guarantees this) so handlers are not duplicated.
    """
    logger = logging.getLogger(name)

    if logger.handlers:
        return logger

    level = getattr(logging, LOG_LEVEL.upper(), logging.DEBUG)
    logger.setLevel(level)

    fmt = logging.Formatter(
        fmt='%(asctime)s [%(levelname)s] %(name)s — %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    console_handler = logging.StreamHandler()
    console_handler.setLevel(level)
    console_handler.setFormatter(fmt)
    logger.addHandler(console_handler)

    os.makedirs(LOGS_DIR, exist_ok=True)
    debug_log_path = os.path.join(LOGS_DIR, 'debug.log')
    file_handler = logging.FileHandler(debug_log_path, mode='a', encoding='utf-8')
    file_handler.setLevel(level)
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    logger.propagate = False

    return logger

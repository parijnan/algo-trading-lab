"""
logger_setup.py — Athena Production Logging Setup
Configures a logger that writes to both console and logs/debug.log.
"""

import os
import logging
from configs_live import LOG_LEVEL, LOGS_DIR


def get_logger(name: str) -> logging.Logger:
    """
    Return a logger configured with console and file handlers.
    """
    logger = logging.getLogger(name)

    # Only configure if not already set up
    if logger.handlers:
        return logger

    level = getattr(logging, LOG_LEVEL.upper(), logging.DEBUG)
    logger.setLevel(level)

    fmt = logging.Formatter(
        fmt='%(asctime)s [%(levelname)s] %(name)s — %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    # Console handler
    console_handler = logging.StreamHandler()
    console_handler.setLevel(level)
    console_handler.setFormatter(fmt)
    logger.addHandler(console_handler)

    # File handler — logs/debug.log
    os.makedirs(LOGS_DIR, exist_ok=True)
    debug_log_path = os.path.join(LOGS_DIR, 'debug.log')
    file_handler = logging.FileHandler(debug_log_path, mode='a', encoding='utf-8')
    file_handler.setLevel(level)
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    # Prevent propagation to root logger
    logger.propagate = False

    return logger

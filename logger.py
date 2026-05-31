"""
logger.py
---------
Shared logging configuration. Writes to both stdout and a timestamped file
in the logs/ directory so GitHub Actions can archive the artifact.
"""

import logging
import os
from datetime import datetime, timezone

import config

_CONFIGURED = False


def get_logger(name: str = "bookee_sync") -> logging.Logger:
    global _CONFIGURED
    logger = logging.getLogger(name)
    if _CONFIGURED:
        return logger

    os.makedirs(config.LOGS_DIR, exist_ok=True)
    log_file = os.path.join(
        config.LOGS_DIR,
        f"sync_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.log",
    )

    fmt = logging.Formatter(
        "[%(levelname)s] %(asctime)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    stream = logging.StreamHandler()
    stream.setFormatter(fmt)

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(fmt)

    logger.setLevel(logging.INFO)
    logger.addHandler(stream)
    logger.addHandler(file_handler)
    logger.propagate = False

    _CONFIGURED = True
    return logger

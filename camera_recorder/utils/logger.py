import os
import sys
import logging
from datetime import datetime
from pathlib import Path

LOG_LEVEL_MAP = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}

class LoggerManager:
    _instances = {}

    @classmethod
    def get_logger(cls, name, log_dir=None, level="INFO"):
        key = (name, log_dir)
        if key in cls._instances:
            return cls._instances[key]

        logger = logging.getLogger(name)
        logger.setLevel(LOG_LEVEL_MAP.get(level.upper(), logging.INFO))
        logger.handlers.clear()

        formatter = logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"
        )

        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(logging.DEBUG)
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)

        if log_dir:
            Path(log_dir).mkdir(parents=True, exist_ok=True)
            file_handler = logging.FileHandler(
                os.path.join(log_dir, f"{name}.log"), encoding="utf-8"
            )
            file_handler.setLevel(logging.DEBUG)
            file_handler.setFormatter(formatter)
            logger.addHandler(file_handler)

        logger.propagate = False
        cls._instances[key] = logger
        return logger

    @classmethod
    def log_event(cls, name, event, log_dir=None, level="INFO", details=""):
        logger = cls.get_logger(name, log_dir, level)
        msg = f"{event}" + (f" | {details}" if details else "")
        logger.log(LOG_LEVEL_MAP.get(level.upper(), logging.INFO), msg)

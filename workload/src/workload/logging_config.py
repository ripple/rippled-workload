import logging
import logging.config
import os
import sys

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

LOGGING_CONFIG = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "console": {
            "format": "%(asctime)s %(levelname)-6s %(name)8s:%(lineno)d %(message)s",
            "datefmt": "%Y-%m-%d %H:%M:%S",
        },
        "file": {
            "format": "%(asctime)s %(levelname)-6s %(name)s:%(lineno)d %(message)s",
            "datefmt": "%Y-%m-%dT%H:%M:%S%z",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "console",
            "level": LOG_LEVEL,
            "stream": sys.stdout,
        },
        "file": {
            "class": "logging.handlers.RotatingFileHandler",
            "formatter": "file",
            "level": "DEBUG",
            "filename": "workload.log",
            "maxBytes": 50_000_000,  # 50 MB
            "backupCount": 5,
        },
    },
    "loggers": {
        "workload": {
            "level": "DEBUG",
            "handlers": ["console", "file"],
            "propagate": False,
        },
        "fastapi": {
            "level": "INFO",
            "handlers": ["console", "file"],
            "propagate": False,
        },
        "uvicorn.access": {
            "level": "WARNING",
            "handlers": ["console", "file"],
            "propagate": False,
        },
        "xrpl": {
            "level": "WARNING",
            "handlers": ["console", "file"],
            "propagate": False,
        },
    },
    "root": {
        "level": "WARNING",
        "handlers": ["console", "file"],
    },
}


def setup_logging():
    """Apply the logging configuration, rotating the old log file first."""
    # Rotate existing log so each run starts fresh
    log_path = LOGGING_CONFIG["handlers"]["file"]["filename"]
    if os.path.exists(log_path):
        from logging.handlers import RotatingFileHandler

        rotator = RotatingFileHandler(
            log_path,
            maxBytes=0,  # force rotation regardless of size
            backupCount=LOGGING_CONFIG["handlers"]["file"]["backupCount"],
        )
        rotator.doRollover()
        rotator.close()

    logging.config.dictConfig(LOGGING_CONFIG)
    os.environ.setdefault("PYTHONUNBUFFERED", "1")

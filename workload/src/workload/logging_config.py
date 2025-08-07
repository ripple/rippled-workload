import logging
import logging.config
import os
import sys

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

LOGGING_CONFIG = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "default": {
            "format": "%(asctime)s %(levelname)-6s %(name)8s:%(lineno)d %(message)s",
            "datefmt": "%Y-%m-%d %H:%M:%S",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "default",
            "stream": sys.stdout,
        },
        "file": {
            "class": "logging.FileHandler",
            "formatter": "default",
            "filename": "/tmp/workload.log",
            "mode": "a",
        },
    },
    "loggers": {
        "workload": {
            "level": LOG_LEVEL,
            "handlers": ["console", "file"],
            "propagate": False,  # Don't pass 'workload' logs up to the root logger
        },
        "fastapi": {
            "level": "INFO",
            "handlers": ["console", "file"],
            "propagate": False,
        },
        "uvicorn.access": {
            "level": "WARNING",  # Quiets the noisy access logs
            "handlers": ["console", "file"],
            "propagate": False,
        },
        "xrpl": {
            "level": "WARNING",  # Only show warnings/errors from xrpl-py
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
    """Apply the logging configuration."""
    logging.config.dictConfig(LOGGING_CONFIG)
    os.environ.setdefault("PYTHONUNBUFFERED", "1")

import json
import logging
import logging.config
import pathlib
from datetime import UTC, datetime
from typing import Any

pkg_root = pathlib.Path(__file__).parent
config_file = pkg_root / "config.json"
log_path = pathlib.Path("logs")

with config_file.open() as f_in:
    conf_file = json.load(f_in)

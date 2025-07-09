import json
import logging
import logging.config
import pathlib
from datetime import UTC, datetime
from typing import Any
from generate_ledger import generate_compose_file, generate_ledger_file

pkg_root = pathlib.Path(__file__).parent
config_file = pkg_root / "config.json"
log_path = pathlib.Path("logs")

with config_file.open() as f_in:
    conf_file = json.load(f_in)

def setup_logging(logging_config: dict[str, Any]) -> None:
    logfile_name = f"{pkg_root.name}.log"
    timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    log_file = log_path / f"{timestamp}-{logfile_name}"
    logging_config["handlers"]["file"]["filename"] = log_file
    log_path.mkdir(parents=True, exist_ok=True)
    logging.config.dictConfig(logging_config)
    logging.getLogger("urllib3").propagate = False
    logging.getLogger("requests").propagate = False
    logging.getLogger("anyio").propagate = False
    logging.getLogger("httpcore").propagate = False
    logging.getLogger("asyncio").propagate = False
    logging.getLogger("httpx").propagate = False
    # logging.getLogger("xrpl-py").propagate = False


setup_logging(conf_file["logging"])
logger = logging.getLogger(__name__)

def main():
    generate_ledger_file()
    generate_compose_file()

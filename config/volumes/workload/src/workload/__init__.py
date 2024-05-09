import datetime
import logging
from pathlib import Path

FORMAT = "%(asctime)s [%(levelname)s] - %(name)+15s:%(lineno)-3d - %(message)s"
FORMAT_CONSOLE = "%(asctime)s [%(levelname)s] - %(name)+15s:%(lineno)-3d - %(message)s"
LOG_TO_FILE = False

class CustomFormatter(logging.Formatter):

    bold_green = "\x1b[31;1m"
    bold_red = "\x1b[31;1m"
    bold_white = "\x1b[37;1m"
    grey = "\x1b[38;20m"
    red = "\x1b[31;20m"
    blue = "\x1b[34;20m"
    magenta = "\x1b[35;20m"
    cyan = "\x1b[36;20m"
    white = "\x1b[37;20m"
    yellow = "\x1b[33;20m"
    reset = "\x1b[0m"
    format = FORMAT

    FORMATS = {
        logging.DEBUG: grey + format + reset,
        logging.INFO: reset + format + reset,
        logging.WARNING: yellow + format + reset,
        logging.ERROR: red + format + reset,
        logging.CRITICAL: bold_red + format + reset,
    }

    def format(self, record):
        log_fmt = self.FORMATS.get(record.levelno)
        formatter = logging.Formatter(log_fmt)
        formatter.default_msec_format = "%s.%04d"
        return formatter.format(record)


root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)

running_in_docker = Path("/.dockerenv").is_file()

if LOG_TO_FILE or not running_in_docker:
    logdir = Path("/var/log/workload/") if running_in_docker else Path(__file__).parent
    Path(logdir).mkdir(parents=True, exist_ok=True)
    tstamp = datetime.datetime.now().strftime("%Y_%j_%H_%M_%S")

    dirname, logfile, suffix = logdir, tstamp, ".log"
    logfile = Path(dirname, logfile).with_suffix(suffix)
    file_level = logging.DEBUG

    file = logging.FileHandler(logfile)
    file.setLevel(file_level)
    file_formatter = logging.Formatter(fmt=FORMAT)
    file_formatter.default_msec_format = "%s.%04d"
    file.setFormatter(file_formatter)
    root_logger.addHandler(file)


console_level = logging.INFO

console = logging.StreamHandler()
console.setLevel(console_level)
console.setFormatter(CustomFormatter())

root_logger.addHandler(console)

log = logging.getLogger(__name__)

logging.getLogger("urllib3").setLevel(logging.DEBUG)
logging.getLogger("requests").setLevel(logging.DEBUG)
logging.getLogger("websockets").setLevel(logging.DEBUG)
logging.getLogger("asyncio").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)

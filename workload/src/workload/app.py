from contextlib import asynccontextmanager
from fastapi import FastAPI
from workload.logging_config import setup_logging
import logging

try:
    from antithesis.lifecycle import setup_complete
    ANTITHESIS_AVAILABLE = True
except ImportError:
    ANTITHESIS_AVAILABLE = False

    def setup_complete(details=None):
        pass

setup_logging()
log = logging.getLogger("workload.app")

@asynccontextmanager
async def lifespan(app: FastAPI):
    msg = "Network is ready. Initializing workload..."
    print(msg)
    log.info(msg)
    setup_complete({"message": msg})
    yield

app = FastAPI(
    title="XRPL Workload",
    debug=True,
    lifespan=lifespan,
)

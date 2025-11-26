from contextlib import asynccontextmanager
from fastapi import FastAPI

try:
    from antithesis.lifecycle import setup_complete
    ANTITHESIS_AVAILABLE = True
except ImportError:
    ANTITHESIS_AVAILABLE = False

    def setup_complete(details=None):
        pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    msg = "Network is ready. Initializing workload..."
    print(msg)
    setup_complete({"message": msg})
    print("lifecycle msg sent")
    yield

app = FastAPI(
    title="XRPL Workload",
    debug=True,
    lifespan=lifespan,
)

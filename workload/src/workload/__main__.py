import asyncio
from contextlib import suppress

import uvicorn


def main():
    uvicorn.run("workload.app:app", host="0.0.0.0", port=8000, lifespan="on")

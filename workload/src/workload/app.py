import json
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from antithesis import lifecycle
from antithesis._internal import _HANDLER
from antithesis.assertions import always, reachable, unreachable
from fastapi import Depends, FastAPI
from xrpl.asyncio.clients import AsyncJsonRpcClient
from xrpl.constants import CryptoAlgorithm, XRPLException
from xrpl.wallet import Wallet

from workload import logger
from workload.assertions import register_assertions
from workload.check_xrpld_sync_state import is_xrpld_synced
from workload.config import conf_file, config_file
from workload.models import UserAccount
from workload.transactions import REGISTRY


class Workload:
    def __init__(self, conf: dict[str, Any]):
        self.config = conf
        self.accounts = {}
        self.gateways = []
        self.amms = []
        self.nfts = []
        self.nft_offers = []
        self.currencies = []
        self.trust_lines = []
        self.credentials = []
        self.vaults = []
        self.domains = []
        self.mpt_issuances = []
        self.delegates = []
        self.loan_brokers = []
        self.loans = []
        self.escrows = []
        self.checks = []
        self.payment_channels = []
        self.deleted_vault_ids: list[str] = []
        self.deleted_broker_ids: list[str] = []
        self.deleted_loan_ids: list[str] = []
        self.funding_wallet: Wallet = None
        self.failures = []
        self.currency_codes = conf["currencies"]["codes"]
        self.default_balance = conf["accounts"]["default_balance"]
        self.start_time = time.time()
        xrpld_host = os.environ.get("XRPLD_NAME", conf["xrpld"]["local"])
        xrpld_rpc_port = os.environ.get("XRPLD_RPC_PORT", conf["xrpld"]["json_rpc_port"])
        xrpld_ws_port = os.environ.get("XRPLD_WS_PORT", conf["xrpld"]["ws_port"])
        self.xrpld = f"http://{xrpld_host}:{xrpld_rpc_port}"
        self.xrpld_ws = f"ws://{xrpld_host}:{xrpld_ws_port}"
        logger.info("Connecting to xrpld at: %s", self.xrpld)

        accounts_json = Path(os.environ.get("ACCOUNTS_JSON", "/accounts.json"))
        if not accounts_json.exists():
            logger.error(
                "accounts.json not found at %s. Cannot start without pre-generated accounts.",
                accounts_json,
            )
            unreachable("workload::accounts_json_missing", {"path": str(accounts_json)})
        else:
            self.load_initial_accounts(accounts_json)
            reachable("workload::accounts_ready", {"count": len(self.accounts)})

        default_algo = CryptoAlgorithm[
            conf_file["workload"]["accounts"]["default_crypto_algorithm"]
        ]
        self.funding_wallet = Wallet.from_seed(
            self.config["genesis_account"]["master_seed"], algorithm=default_algo
        )
        self.client = AsyncJsonRpcClient(self.xrpld)

        from workload.sequence import SequenceTracker

        self.seq = SequenceTracker(self.client)

        self.wait_for_network(self.xrpld)

        # Wire delegation state so submit_tx can transparently delegate.
        from workload.submit import configure as configure_submit
        configure_submit(self.delegates, self.accounts)

        logger.info("Antithesis SDK handler: %s", type(_HANDLER).__name__)
        reachable("workload::started", {})
        always(True, "workload::sdk_works", {"message": "SDK canary assertion"})

        register_assertions()

        logger.info("Workload initialized after %ss", int(time.time() - self.start_time))

    def load_initial_accounts(self, accounts_json: Path) -> None:
        """Load pre-generated accounts from a JSON file.

        Expects format: [{"address": "r...", "seed": "s..."}, ...]
        """
        logger.info(f"Loading accounts from {accounts_json}")
        accounts = json.loads(accounts_json.read_text())
        default_algo = CryptoAlgorithm[
            conf_file["workload"]["accounts"]["default_crypto_algorithm"]
        ]
        for entry in accounts:
            wallet = Wallet.from_seed(seed=entry["seed"], algorithm=default_algo)
            self.accounts[wallet.address] = UserAccount(wallet=wallet)
        logger.info(f"Loaded {len(self.accounts)} accounts")

    def wait_for_network(self, xrpld: str) -> None:
        """Wait for xrpld to be stably synced (multiple consecutive checks).

        A single 'full' response can be a transient moment during validator
        convergence. Requiring consecutive successes ensures the snapshot
        taken after setup_complete() captures a stable network state.
        """
        timeout = self.config["xrpld"]["timeout"]
        wait_start = time.time()
        required_consecutive = 3
        consecutive_ok = 0
        while consecutive_ok < required_consecutive:
            if is_xrpld_synced(xrpld):
                consecutive_ok += 1
                logger.info("Sync check %d/%d passed", consecutive_ok, required_consecutive)
            else:
                if consecutive_ok > 0:
                    logger.info("Sync check failed after %d consecutive, resetting", consecutive_ok)
                consecutive_ok = 0
            if time.time() - wait_start > timeout:
                logger.error("xrpld not stably synced after %ds, proceeding anyway", timeout)
                break
            if consecutive_ok < required_consecutive:
                time.sleep(2)
        logger.info("xrpld stably synced after %ds", int(time.time() - wait_start))


def _make_endpoint(path: str, name: str, handler_fn: Callable, args_fn: Callable) -> Callable:
    """Create an endpoint handler with standardized error handling."""

    async def endpoint(w: Workload = Depends(get_workload)):
        try:
            return await handler_fn(*args_fn(w))
        except (XRPLException, httpx.TimeoutException) as e:
            logger.warning(f"{name}: {type(e).__name__}: {e}")
        except Exception as e:
            logger.error(f"{name} failed: {type(e).__name__}: {e}")
            unreachable(
                "workload::endpoint_exception",
                {"endpoint": path, "error": f"{type(e).__name__}: {e}"},
            )

    endpoint.__name__ = f"{name}_endpoint"
    return endpoint


# Module-level ref needed by _make_endpoint's Depends
get_workload = None


def create_app(workload: Workload) -> FastAPI:
    import asyncio
    from contextlib import asynccontextmanager

    from workload.ws_listener import start_ws_listener

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        from workload.setup import run_setup

        asyncio.create_task(start_ws_listener(workload, workload.xrpld_ws))
        await asyncio.sleep(1)  # let WS listener connect before setup submits

        try:
            result = await run_setup(workload)
            reachable("workload::setup_complete_with_state", result)
        except Exception as e:
            logger.error(f"setup failed: {type(e).__name__}: {e}")
            unreachable(
                "workload::setup_failed",
                {"error": f"{type(e).__name__}: {e}"},
            )

        lifecycle.setup_complete(details={"message": "Setup complete, faults may begin"})
        yield  # app runs here, shutdown after yield

    app = FastAPI(lifespan=lifespan)

    global get_workload

    def get_workload():
        return workload

    # Register all transaction endpoints from the registry
    for name, path, handler_fn, args_fn, _ in REGISTRY:
        app.get(path)(_make_endpoint(path, name, handler_fn, args_fn))

    return app


def main() -> None:
    logger.info("Loaded config from %s", config_file)
    conf = conf_file["workload"]
    logger.info("Config %s", json.dumps(conf, indent=2))
    workload = Workload(conf)
    app = create_app(workload)
    uvicorn.run(app, host="0.0.0.0", port=8000)

import json
import os
import time
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from antithesis import lifecycle
from fastapi import FastAPI, Depends
from xrpl.asyncio.clients import AsyncJsonRpcClient
from xrpl.constants import CryptoAlgorithm, XRPLException
from xrpl.models import IssuedCurrency
from xrpl.models.requests import ServerInfo, Fee
from xrpl.wallet import Wallet

from workload import logger
from workload.assertions import register_assertions
from antithesis.assertions import always, reachable, unreachable
from antithesis._internal import _HANDLER
from workload.check_xrpld_sync_state import is_xrpld_synced
from workload.config import conf_file, config_file
from workload.models import UserAccount
from workload.nft import nftoken_mint, nftoken_burn, nftoken_modify, nftoken_create_offer, nftoken_cancel_offer, nftoken_accept_offer
from workload.payments import payment_random as payment_random_fn
from workload.trustlines import trustline_create
from workload.account_set import account_set_random
from workload.tickets import ticket_create, ticket_use
from workload.batch import batch_random
from workload.credentials import credential_create, credential_accept, credential_delete
from workload.vaults import vault_create, vault_deposit, vault_withdraw, vault_set, vault_delete, vault_clawback
from workload.domains import permissioned_domain_set, permissioned_domain_delete
from workload.delegation import delegate_set
from workload.mpt import mpt_create, mpt_authorize, mpt_issuance_set, mpt_destroy
from workload.lending import (
    loan_broker_set, loan_broker_delete,
    loan_broker_cover_deposit, loan_broker_cover_withdraw,
    loan_set, loan_delete, loan_manage, loan_pay,
)

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
        self.loan_brokers = []
        self.loans = []
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
            logger.error("accounts.json not found at %s. Cannot start without pre-generated accounts.", accounts_json)
            unreachable("workload::accounts_json_missing", {"path": str(accounts_json)})
        else:
            self.load_initial_accounts(accounts_json)
            reachable("workload::accounts_ready", {"count": len(self.accounts)})

        default_algo = CryptoAlgorithm[conf_file["workload"]["accounts"]["default_crypto_algorithm"]]
        self.funding_wallet = Wallet.from_seed(self.config["genesis_account"]["master_seed"], algorithm=default_algo)
        self.client = AsyncJsonRpcClient(self.xrpld)

        self.wait_for_network(self.xrpld)

        logger.info("Antithesis SDK handler: %s", type(_HANDLER).__name__)
        reachable("workload::started", {})
        always(True, "workload::sdk_works", {"message": "SDK canary assertion"})

        register_assertions()

        workload_ready_msg = "Workload initialization complete"
        logger.info("%s after %ss", workload_ready_msg, int(time.time() - self.start_time))
        lifecycle.setup_complete(details={"message": workload_ready_msg})

    @property
    def addresses(self):
        return list(self.accounts.keys())

    async def server_info(self):
        server_info_response = await self.client.request(ServerInfo())
        return server_info_response.result["info"]

    async def fee(self):
        fee_response = await self.client.request(Fee())
        return fee_response.result

    async def get_ref_fee(self):
        fee_response = await self.fee()
        return fee_response["drops"]["base_fee"]

    async def get_open_ledger_fee(self):
        fee_response = await self.fee()
        return fee_response["drops"]["open_ledger_fee"]

    async def expected_ledger_size(self):
        fee_response = await self.fee()
        return int(fee_response["expected_ledger_size"])

    def load_initial_accounts(self, accounts_json: Path):
        """Load pre-generated accounts from a JSON file.

        Expects format: [{"address": "r...", "seed": "s..."}, ...]
        """
        logger.info(f"Loading accounts from {accounts_json}")
        accounts = json.loads(accounts_json.read_text())
        default_algo = CryptoAlgorithm[conf_file["workload"]["accounts"]["default_crypto_algorithm"]]
        for entry in accounts:
            wallet = Wallet.from_seed(seed=entry["seed"], algorithm=default_algo)
            self.accounts[wallet.address] = UserAccount(wallet=wallet)
        logger.info(f"Loaded {len(self.accounts)} accounts")

    @classmethod
    def issue_currencies(cls, issuer: str, currency_code: list[str]) -> list[IssuedCurrency]:
        issued_currencies = [IssuedCurrency.from_dict(dict(issuer=issuer, currency=cc)) for cc in currency_code]
        logger.debug("Issued %s currencies", len(issued_currencies))
        return issued_currencies

    def wait_for_network(self, xrpld) -> None:
        timeout = self.config["xrpld"]["timeout"]
        wait_start = time.time()
        logger.debug("Waiting %ss for xrpld at %s to be running.", timeout, xrpld)
        while not (is_xrpld_synced(xrpld)):
            irs = is_xrpld_synced(xrpld)
            logger.info(f"is_xrpld_synced returning: {irs}")
            if (xrpld_ready_time := int(time.time() - self.start_time)) > timeout:
                logger.info("xrpld ready after %ss", xrpld_ready_time)
            logger.info("Waited %ss so far", int(time.time() - wait_start))
            time.sleep(10)
        logger.info("xrpld ready...")

    def get_accounts(self):
        return self.accounts

    def get_nfts(self):
        return self.nfts


# ── Transaction endpoint definitions ─────────────────────────────────
# Each entry: (path, name, handler_fn, args_fn)
# args_fn takes a Workload and returns the args tuple to pass to handler_fn.

_ENDPOINTS = [
    # NFT
    ("/nft/mint/random",         "nft_mint",         nftoken_mint,         lambda w: (w.accounts, w.nfts, w.client)),
    ("/nft/burn/random",         "nft_burn",         nftoken_burn,         lambda w: (w.accounts, w.nfts, w.client)),
    ("/nft/modify/random",       "nft_modify",       nftoken_modify,       lambda w: (w.accounts, w.nfts, w.client)),
    ("/nft/create_offer/random", "nft_create_offer", nftoken_create_offer, lambda w: (w.accounts, w.nfts, w.nft_offers, w.client)),
    ("/nft/cancel_offer/random", "nft_cancel_offer", nftoken_cancel_offer, lambda w: (w.accounts, w.nft_offers, w.client)),
    ("/nft/accept_offer/random", "nft_accept_offer", nftoken_accept_offer, lambda w: (w.accounts, w.nfts, w.nft_offers, w.client)),
    # Account
    ("/account/set/random",      "account_set",      account_set_random,   lambda w: (w.accounts, w.client)),
    # Trust Lines
    ("/trustline/create/random", "trustline_create",  trustline_create,    lambda w: (w.accounts, w.trust_lines, w.client)),
    # Payments
    ("/payment/random",          "payment_random",    payment_random_fn,   lambda w: (w.accounts, w.trust_lines, w.mpt_issuances, w.client)),
    # Tickets
    ("/tickets/create/random",   "ticket_create",     ticket_create,       lambda w: (w.accounts, w.client)),
    ("/tickets/use/random",      "ticket_use",        ticket_use,          lambda w: (w.accounts, w.client)),
    # Batch
    ("/batch/random",            "batch_random",      batch_random,        lambda w: (w.accounts, w.client)),
    # MPToken
    ("/mpt/create/random",       "mpt_create",        mpt_create,          lambda w: (w.accounts, w.mpt_issuances, w.client)),
    ("/mpt/authorize/random",    "mpt_authorize",     mpt_authorize,       lambda w: (w.accounts, w.mpt_issuances, w.client)),
    ("/mpt/set/random",          "mpt_set",           mpt_issuance_set,    lambda w: (w.accounts, w.mpt_issuances, w.client)),
    ("/mpt/destroy/random",      "mpt_destroy",       mpt_destroy,         lambda w: (w.accounts, w.mpt_issuances, w.client)),
    # Credentials
    ("/credential/create/random", "credential_create", credential_create,  lambda w: (w.accounts, w.credentials, w.client)),
    ("/credential/accept/random", "credential_accept", credential_accept,  lambda w: (w.accounts, w.credentials, w.client)),
    ("/credential/delete/random", "credential_delete", credential_delete,  lambda w: (w.accounts, w.credentials, w.client)),
    # Vaults
    ("/vault/create/random",     "vault_create",      vault_create,        lambda w: (w.accounts, w.vaults, w.trust_lines, w.mpt_issuances, w.client)),
    ("/vault/deposit/random",    "vault_deposit",     vault_deposit,       lambda w: (w.accounts, w.vaults, w.client)),
    ("/vault/withdraw/random",   "vault_withdraw",    vault_withdraw,      lambda w: (w.accounts, w.vaults, w.client)),
    ("/vault/set/random",        "vault_set",         vault_set,           lambda w: (w.accounts, w.vaults, w.client)),
    ("/vault/delete/random",     "vault_delete",      vault_delete,        lambda w: (w.accounts, w.vaults, w.client)),
    ("/vault/clawback/random",   "vault_clawback",    vault_clawback,      lambda w: (w.accounts, w.vaults, w.client)),
    # Permissioned Domains
    ("/domain/set/random",       "domain_set",        permissioned_domain_set,    lambda w: (w.accounts, w.domains, w.client)),
    ("/domain/delete/random",    "domain_delete",     permissioned_domain_delete, lambda w: (w.accounts, w.domains, w.client)),
    # Delegation
    ("/delegate/set/random",     "delegate_set",      delegate_set,        lambda w: (w.accounts, w.client)),
    # Lending
    ("/loan/broker/set/random",             "loan_broker_set",             loan_broker_set,             lambda w: (w.accounts, w.vaults, w.loan_brokers, w.client)),
    ("/loan/broker/delete/random",          "loan_broker_delete",          loan_broker_delete,          lambda w: (w.accounts, w.loan_brokers, w.client)),
    ("/loan/broker/cover/deposit/random",   "loan_broker_cover_deposit",   loan_broker_cover_deposit,   lambda w: (w.accounts, w.loan_brokers, w.client)),
    ("/loan/broker/cover/withdraw/random",  "loan_broker_cover_withdraw",  loan_broker_cover_withdraw,  lambda w: (w.accounts, w.loan_brokers, w.client)),
    ("/loan/set/random",                    "loan_set",                    loan_set,                    lambda w: (w.accounts, w.loan_brokers, w.loans, w.client)),
    ("/loan/delete/random",                 "loan_delete",                 loan_delete,                 lambda w: (w.accounts, w.loans, w.client)),
    ("/loan/manage/random",                 "loan_manage",                 loan_manage,                 lambda w: (w.accounts, w.loan_brokers, w.loans, w.client)),
    ("/loan/pay/random",                    "loan_pay",                    loan_pay,                    lambda w: (w.accounts, w.loans, w.client)),
]


def _make_endpoint(path: str, name: str, handler_fn, args_fn):
    """Create an endpoint handler with standardized error handling."""
    async def endpoint(w: Workload = Depends(get_workload)):
        try:
            return await handler_fn(*args_fn(w))
        except (XRPLException, httpx.TimeoutException) as e:
            logger.warning(f"{name}: {type(e).__name__}: {e}")
        except Exception as e:
            logger.error(f"{name} failed: {type(e).__name__}: {e}")
            unreachable("workload::endpoint_exception", {"endpoint": path, "error": f"{type(e).__name__}: {e}"})
    endpoint.__name__ = f"{name}_endpoint"
    return endpoint


# Module-level ref needed by _make_endpoint's Depends
get_workload = None


def create_app(workload: Workload) -> FastAPI:
    import asyncio
    from workload.ws_listener import start_ws_listener

    app = FastAPI()

    global get_workload
    def get_workload():
        return workload

    @app.on_event("startup")
    async def startup():
        asyncio.create_task(start_ws_listener(workload, workload.xrpld_ws))

    # Setup endpoint (special — has its own logic)
    @app.get("/setup")
    async def setup_endpoint(w: Workload = Depends(get_workload)):
        from workload.setup import run_setup
        try:
            result = await run_setup(w)
            reachable("workload::setup_complete_with_state", result)
            return result
        except (XRPLException, httpx.TimeoutException) as e:
            logger.warning(f"setup: {type(e).__name__}: {e}")
        except Exception as e:
            logger.error(f"setup failed: {type(e).__name__}: {e}")
            unreachable("workload::endpoint_exception", {"endpoint": "/setup", "error": f"{type(e).__name__}: {e}"})

    # Info endpoints
    @app.get("/accounts")
    def get_accounts(w: Workload = Depends(get_workload)):
        return w.addresses

    @app.get("/nft/list")
    def get_nfts(w: Workload = Depends(get_workload)):
        return w.get_nfts()

    # Register all transaction endpoints from the table
    for path, name, handler_fn, args_fn in _ENDPOINTS:
        app.get(path)(_make_endpoint(path, name, handler_fn, args_fn))

    return app


def main():
    logger.info("Loaded config from %s", config_file)
    conf = conf_file["workload"]
    logger.info("Config %s", json.dumps(conf, indent=2))
    workload = Workload(conf)
    app = create_app(workload)
    uvicorn.run(app, host="0.0.0.0", port=8000)

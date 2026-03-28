import json
import os
import time
from pathlib import Path
from typing import Any

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
        # TODO: Lookup account by nfts owned, tickets, etc
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
        # print('{"antithesis_setup": { "status": "complete", "details": "" }}')
        # logger.info("Called lifecycle setup_complete()")

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
        """Use a fixed set of currency codes to create IssuedCurrencies for a specific gateway.

        Args:
            issuer (str): Account_id of the gateway for all currencies
            currency_code (list[str], optional): _description_. Defaults to config.currency_codes.

        Returns:
            list[IssuedCurrency]: List of IssuedCurrencies a gateway provides

        """
        issued_currencies = [IssuedCurrency.from_dict(dict(issuer=issuer, currency=cc)) for cc in currency_code]
        logger.debug("Issued %s currencies", len(issued_currencies))
        if True:
            for c in issued_currencies:
                logger.debug(c)
        return issued_currencies

    def wait_for_network(self, xrpld) -> None:
        timeout = self.config["xrpld"]["timeout"]  # Wait at most 10 minutes
        wait_start = time.time()
        logger.debug("Waiting %ss for xrpld at %s to be running.", timeout, xrpld)
        while not (is_xrpld_synced(xrpld)):
            irs = is_xrpld_synced(xrpld)
            logger.info(f"is_xrpld_synced returning: {irs}")

            if (xrpld_ready_time := int(time.time() - self.start_time)) > timeout:
                logger.info("xrpld ready after %ss", xrpld_ready_time)
            logger.info("Waited %ss so far", int(time.time() - wait_start))
            wait_time = 10
            time.sleep(wait_time)
        logger.info("xrpld ready...")

    def get_accounts(self):
        return self.accounts

    def get_nfts(self):
        return self.nfts

def create_app(workload: Workload) -> FastAPI:
    import asyncio
    from workload.ws_listener import start_ws_listener

    app = FastAPI()

    @app.on_event("startup")
    async def startup():
        asyncio.create_task(start_ws_listener(workload, workload.xrpld_ws))

    def get_workload():
        return workload

    @app.get("/setup")
    async def setup_endpoint(w: Workload = Depends(get_workload)):
        from workload.setup import run_setup
        try:
            result = await run_setup(w)
            reachable("workload::setup_complete_with_state", result)
            return result
        except XRPLException as e:
            logger.warning(f"setup: {type(e).__name__}: {e}")
        except Exception as e:
            logger.error(f"setup failed: {type(e).__name__}: {e}")
            unreachable("workload::endpoint_exception", {"endpoint": "/setup", "error": f"{type(e).__name__}: {e}"})

    @app.get("/accounts")
    def get_accounts(w: Workload = Depends(get_workload)):
        return w.addresses

    @app.get("/nft/list")
    def get_nfts(w: Workload = Depends(get_workload)):
        return w.get_nfts()

    # ── NFT ──────────────────────────────────────────────────────
    @app.get("/nft/mint/random")
    async def nft_mint_endpoint(w: Workload = Depends(get_workload)):
        try:
            return await nftoken_mint(w.accounts, w.nfts, w.client)
        except XRPLException as e:
            logger.warning(f"nft_mint: {type(e).__name__}: {e}")
        except Exception as e:
            logger.error(f"nft_mint failed: {type(e).__name__}: {e}")
            unreachable("workload::endpoint_exception", {"endpoint": "/nft/mint/random", "error": f"{type(e).__name__}: {e}"})

    @app.get("/nft/burn/random")
    async def nft_burn_endpoint(w: Workload = Depends(get_workload)):
        try:
            return await nftoken_burn(w.accounts, w.nfts, w.client)
        except XRPLException as e:
            logger.warning(f"nft_burn: {type(e).__name__}: {e}")
        except Exception as e:
            logger.error(f"nft_burn failed: {type(e).__name__}: {e}")
            unreachable("workload::endpoint_exception", {"endpoint": "/nft/burn/random", "error": f"{type(e).__name__}: {e}"})

    @app.get("/nft/modify/random")
    async def nft_modify_endpoint(w: Workload = Depends(get_workload)):
        try:
            return await nftoken_modify(w.accounts, w.nfts, w.client)
        except XRPLException as e:
            logger.warning(f"nft_modify: {type(e).__name__}: {e}")
        except Exception as e:
            logger.error(f"nft_modify failed: {type(e).__name__}: {e}")
            unreachable("workload::endpoint_exception", {"endpoint": "/nft/modify/random", "error": f"{type(e).__name__}: {e}"})

    @app.get("/nft/create_offer/random")
    async def nft_create_offer_endpoint(w: Workload = Depends(get_workload)):
        try:
            return await nftoken_create_offer(w.accounts, w.nfts, w.nft_offers, w.client)
        except XRPLException as e:
            logger.warning(f"nft_create_offer: {type(e).__name__}: {e}")
        except Exception as e:
            logger.error(f"nft_create_offer failed: {type(e).__name__}: {e}")
            unreachable("workload::endpoint_exception", {"endpoint": "/nft/create_offer/random", "error": f"{type(e).__name__}: {e}"})

    @app.get("/nft/cancel_offer/random")
    async def nft_cancel_offer_endpoint(w: Workload = Depends(get_workload)):
        try:
            return await nftoken_cancel_offer(w.accounts, w.nft_offers, w.client)
        except XRPLException as e:
            logger.warning(f"nft_cancel_offer: {type(e).__name__}: {e}")
        except Exception as e:
            logger.error(f"nft_cancel_offer failed: {type(e).__name__}: {e}")
            unreachable("workload::endpoint_exception", {"endpoint": "/nft/cancel_offer/random", "error": f"{type(e).__name__}: {e}"})

    @app.get("/nft/accept_offer/random")
    async def nft_accept_offer_endpoint(w: Workload = Depends(get_workload)):
        try:
            return await nftoken_accept_offer(w.accounts, w.nfts, w.nft_offers, w.client)
        except XRPLException as e:
            logger.warning(f"nft_accept_offer: {type(e).__name__}: {e}")
        except Exception as e:
            logger.error(f"nft_accept_offer failed: {type(e).__name__}: {e}")
            unreachable("workload::endpoint_exception", {"endpoint": "/nft/accept_offer/random", "error": f"{type(e).__name__}: {e}"})

    # ── Account Set ───────────────────────────────────────────────
    @app.get("/account/set/random")
    async def account_set_endpoint(w: Workload = Depends(get_workload)):
        try:
            return await account_set_random(w.accounts, w.client)
        except XRPLException as e:
            logger.warning(f"account_set: {type(e).__name__}: {e}")
        except Exception as e:
            logger.error(f"account_set failed: {type(e).__name__}: {e}")
            unreachable("workload::endpoint_exception", {"endpoint": "/account/set/random", "error": f"{type(e).__name__}: {e}"})

    # ── Trust Lines ───────────────────────────────────────────────
    @app.get("/trustline/create/random")
    async def trustline_create_endpoint(w: Workload = Depends(get_workload)):
        try:
            return await trustline_create(w.accounts, w.trust_lines, w.client)
        except XRPLException as e:
            logger.warning(f"trustline_create: {type(e).__name__}: {e}")
        except Exception as e:
            logger.error(f"trustline_create failed: {type(e).__name__}: {e}")
            unreachable("workload::endpoint_exception", {"endpoint": "/trustline/create/random", "error": f"{type(e).__name__}: {e}"})

    # ── Payments ─────────────────────────────────────────────────
    @app.get("/payment/random")
    async def payment_endpoint(w: Workload = Depends(get_workload)):
        try:
            return await payment_random_fn(w.accounts, w.trust_lines, w.mpt_issuances, w.client)
        except XRPLException as e:
            logger.warning(f"payment_random: {type(e).__name__}: {e}")
        except Exception as e:
            logger.error(f"payment_random failed: {type(e).__name__}: {e}")
            unreachable("workload::endpoint_exception", {"endpoint": "/payment/random", "error": f"{type(e).__name__}: {e}"})

    # ── Tickets ──────────────────────────────────────────────────
    @app.get("/tickets/create/random")
    async def ticket_create_endpoint(w: Workload = Depends(get_workload)):
        try:
            return await ticket_create(w.accounts, w.client)
        except XRPLException as e:
            logger.warning(f"ticket_create: {type(e).__name__}: {e}")
        except Exception as e:
            logger.error(f"ticket_create failed: {type(e).__name__}: {e}")
            unreachable("workload::endpoint_exception", {"endpoint": "/tickets/create/random", "error": f"{type(e).__name__}: {e}"})

    @app.get("/tickets/use/random")
    async def ticket_use_endpoint(w: Workload = Depends(get_workload)):
        try:
            return await ticket_use(w.accounts, w.client)
        except XRPLException as e:
            logger.warning(f"ticket_use: {type(e).__name__}: {e}")
        except Exception as e:
            logger.error(f"ticket_use failed: {type(e).__name__}: {e}")
            unreachable("workload::endpoint_exception", {"endpoint": "/tickets/use/random", "error": f"{type(e).__name__}: {e}"})

    # ── Batch ────────────────────────────────────────────────────
    @app.get("/batch/random")
    async def batch_endpoint(w: Workload = Depends(get_workload)):
        try:
            return await batch_random(w.accounts, w.client)
        except XRPLException as e:
            logger.warning(f"batch_random: {type(e).__name__}: {e}")
        except Exception as e:
            logger.error(f"batch_random failed: {type(e).__name__}: {e}")
            unreachable("workload::endpoint_exception", {"endpoint": "/batch/random", "error": f"{type(e).__name__}: {e}"})

    # ── MPToken (tracked) ────────────────────────────────────────
    @app.get("/mpt/create/random")
    async def mpt_create_endpoint(w: Workload = Depends(get_workload)):
        try:
            return await mpt_create(w.accounts, w.mpt_issuances, w.client)
        except XRPLException as e:
            logger.warning(f"mpt_create: {type(e).__name__}: {e}")
        except Exception as e:
            logger.error(f"mpt_create failed: {type(e).__name__}: {e}")
            unreachable("workload::endpoint_exception", {"endpoint": "/mpt/create/random", "error": f"{type(e).__name__}: {e}"})

    # ── Credentials ────────────────────────────────────────────────
    @app.get("/credential/create/random")
    async def credential_create_endpoint(w: Workload = Depends(get_workload)):
        try:
            return await credential_create(w.accounts, w.credentials, w.client)
        except XRPLException as e:
            logger.warning(f"credential_create: {type(e).__name__}: {e}")
        except Exception as e:
            logger.error(f"credential_create failed: {type(e).__name__}: {e}")
            unreachable("workload::endpoint_exception", {"endpoint": "/credential/create/random", "error": f"{type(e).__name__}: {e}"})

    @app.get("/credential/accept/random")
    async def credential_accept_endpoint(w: Workload = Depends(get_workload)):
        try:
            return await credential_accept(w.accounts, w.credentials, w.client)
        except XRPLException as e:
            logger.warning(f"credential_accept: {type(e).__name__}: {e}")
        except Exception as e:
            logger.error(f"credential_accept failed: {type(e).__name__}: {e}")
            unreachable("workload::endpoint_exception", {"endpoint": "/credential/accept/random", "error": f"{type(e).__name__}: {e}"})

    @app.get("/credential/delete/random")
    async def credential_delete_endpoint(w: Workload = Depends(get_workload)):
        try:
            return await credential_delete(w.accounts, w.credentials, w.client)
        except XRPLException as e:
            logger.warning(f"credential_delete: {type(e).__name__}: {e}")
        except Exception as e:
            logger.error(f"credential_delete failed: {type(e).__name__}: {e}")
            unreachable("workload::endpoint_exception", {"endpoint": "/credential/delete/random", "error": f"{type(e).__name__}: {e}"})

    # ── Vaults ───────────────────────────────────────────────────
    @app.get("/vault/create/random")
    async def vault_create_endpoint(w: Workload = Depends(get_workload)):
        try:
            return await vault_create(w.accounts, w.vaults, w.trust_lines, w.mpt_issuances, w.client)
        except XRPLException as e:
            logger.warning(f"vault_create: {type(e).__name__}: {e}")
        except Exception as e:
            logger.error(f"vault_create failed: {type(e).__name__}: {e}")
            unreachable("workload::endpoint_exception", {"endpoint": "/vault/create/random", "error": f"{type(e).__name__}: {e}"})

    @app.get("/vault/deposit/random")
    async def vault_deposit_endpoint(w: Workload = Depends(get_workload)):
        try:
            return await vault_deposit(w.accounts, w.vaults, w.client)
        except XRPLException as e:
            logger.warning(f"vault_deposit: {type(e).__name__}: {e}")
        except Exception as e:
            logger.error(f"vault_deposit failed: {type(e).__name__}: {e}")
            unreachable("workload::endpoint_exception", {"endpoint": "/vault/deposit/random", "error": f"{type(e).__name__}: {e}"})

    @app.get("/vault/withdraw/random")
    async def vault_withdraw_endpoint(w: Workload = Depends(get_workload)):
        try:
            return await vault_withdraw(w.accounts, w.vaults, w.client)
        except XRPLException as e:
            logger.warning(f"vault_withdraw: {type(e).__name__}: {e}")
        except Exception as e:
            logger.error(f"vault_withdraw failed: {type(e).__name__}: {e}")
            unreachable("workload::endpoint_exception", {"endpoint": "/vault/withdraw/random", "error": f"{type(e).__name__}: {e}"})

    @app.get("/vault/set/random")
    async def vault_set_endpoint(w: Workload = Depends(get_workload)):
        try:
            return await vault_set(w.accounts, w.vaults, w.client)
        except XRPLException as e:
            logger.warning(f"vault_set: {type(e).__name__}: {e}")
        except Exception as e:
            logger.error(f"vault_set failed: {type(e).__name__}: {e}")
            unreachable("workload::endpoint_exception", {"endpoint": "/vault/set/random", "error": f"{type(e).__name__}: {e}"})

    @app.get("/vault/delete/random")
    async def vault_delete_endpoint(w: Workload = Depends(get_workload)):
        try:
            return await vault_delete(w.accounts, w.vaults, w.client)
        except XRPLException as e:
            logger.warning(f"vault_delete: {type(e).__name__}: {e}")
        except Exception as e:
            logger.error(f"vault_delete failed: {type(e).__name__}: {e}")
            unreachable("workload::endpoint_exception", {"endpoint": "/vault/delete/random", "error": f"{type(e).__name__}: {e}"})

    @app.get("/vault/clawback/random")
    async def vault_clawback_endpoint(w: Workload = Depends(get_workload)):
        try:
            return await vault_clawback(w.accounts, w.vaults, w.client)
        except XRPLException as e:
            logger.warning(f"vault_clawback: {type(e).__name__}: {e}")
        except Exception as e:
            logger.error(f"vault_clawback failed: {type(e).__name__}: {e}")
            unreachable("workload::endpoint_exception", {"endpoint": "/vault/clawback/random", "error": f"{type(e).__name__}: {e}"})

    # ── Permissioned Domains ─────────────────────────────────────
    @app.get("/domain/set/random")
    async def domain_set(w: Workload = Depends(get_workload)):
        try:
            return await permissioned_domain_set(w.accounts, w.domains, w.client)
        except XRPLException as e:
            logger.warning(f"domain_set: {type(e).__name__}: {e}")
        except Exception as e:
            logger.error(f"domain_set failed: {type(e).__name__}: {e}")
            unreachable("workload::endpoint_exception", {"endpoint": "/domain/set/random", "error": f"{type(e).__name__}: {e}"})

    @app.get("/domain/delete/random")
    async def domain_delete(w: Workload = Depends(get_workload)):
        try:
            return await permissioned_domain_delete(w.accounts, w.domains, w.client)
        except XRPLException as e:
            logger.warning(f"domain_delete: {type(e).__name__}: {e}")
        except Exception as e:
            logger.error(f"domain_delete failed: {type(e).__name__}: {e}")
            unreachable("workload::endpoint_exception", {"endpoint": "/domain/delete/random", "error": f"{type(e).__name__}: {e}"})

    # ── Delegation ───────────────────────────────────────────────
    @app.get("/delegate/set/random")
    async def delegate_set_endpoint(w: Workload = Depends(get_workload)):
        try:
            return await delegate_set(w.accounts, w.client)
        except XRPLException as e:
            logger.warning(f"delegate_set: {type(e).__name__}: {e}")
        except Exception as e:
            logger.error(f"delegate_set failed: {type(e).__name__}: {e}")
            unreachable("workload::endpoint_exception", {"endpoint": "/delegate/set/random", "error": f"{type(e).__name__}: {e}"})

    @app.get("/mpt/authorize/random")
    async def mpt_authorize_endpoint(w: Workload = Depends(get_workload)):
        try:
            return await mpt_authorize(w.accounts, w.mpt_issuances, w.client)
        except XRPLException as e:
            logger.warning(f"mpt_authorize: {type(e).__name__}: {e}")
        except Exception as e:
            logger.error(f"mpt_authorize failed: {type(e).__name__}: {e}")
            unreachable("workload::endpoint_exception", {"endpoint": "/mpt/authorize/random", "error": f"{type(e).__name__}: {e}"})

    @app.get("/mpt/set/random")
    async def mpt_set(w: Workload = Depends(get_workload)):
        try:
            return await mpt_issuance_set(w.accounts, w.mpt_issuances, w.client)
        except XRPLException as e:
            logger.warning(f"mpt_set: {type(e).__name__}: {e}")
        except Exception as e:
            logger.error(f"mpt_set failed: {type(e).__name__}: {e}")
            unreachable("workload::endpoint_exception", {"endpoint": "/mpt/set/random", "error": f"{type(e).__name__}: {e}"})

    @app.get("/mpt/destroy/random")
    async def mpt_destroy_endpoint(w: Workload = Depends(get_workload)):
        try:
            return await mpt_destroy(w.accounts, w.mpt_issuances, w.client)
        except XRPLException as e:
            logger.warning(f"mpt_destroy: {type(e).__name__}: {e}")
        except Exception as e:
            logger.error(f"mpt_destroy failed: {type(e).__name__}: {e}")
            unreachable("workload::endpoint_exception", {"endpoint": "/mpt/destroy/random", "error": f"{type(e).__name__}: {e}"})

    # ── Lending Protocol ─────────────────────────────────────────
    @app.get("/loan/broker/set/random")
    async def loan_broker_set_endpoint(w: Workload = Depends(get_workload)):
        try:
            return await loan_broker_set(w.accounts, w.vaults, w.loan_brokers, w.client)
        except XRPLException as e:
            logger.warning(f"loan_broker_set: {type(e).__name__}: {e}")
        except Exception as e:
            logger.error(f"loan_broker_set failed: {type(e).__name__}: {e}")
            unreachable("workload::endpoint_exception", {"endpoint": "/loan/broker/set/random", "error": f"{type(e).__name__}: {e}"})

    @app.get("/loan/broker/delete/random")
    async def loan_broker_delete_endpoint(w: Workload = Depends(get_workload)):
        try:
            return await loan_broker_delete(w.accounts, w.loan_brokers, w.client)
        except XRPLException as e:
            logger.warning(f"loan_broker_delete: {type(e).__name__}: {e}")
        except Exception as e:
            logger.error(f"loan_broker_delete failed: {type(e).__name__}: {e}")
            unreachable("workload::endpoint_exception", {"endpoint": "/loan/broker/delete/random", "error": f"{type(e).__name__}: {e}"})

    @app.get("/loan/broker/cover/deposit/random")
    async def loan_broker_cover_deposit_endpoint(w: Workload = Depends(get_workload)):
        try:
            return await loan_broker_cover_deposit(w.accounts, w.loan_brokers, w.client)
        except XRPLException as e:
            logger.warning(f"loan_broker_cover_deposit: {type(e).__name__}: {e}")
        except Exception as e:
            logger.error(f"loan_broker_cover_deposit failed: {type(e).__name__}: {e}")
            unreachable("workload::endpoint_exception", {"endpoint": "/loan/broker/cover/deposit/random", "error": f"{type(e).__name__}: {e}"})

    @app.get("/loan/broker/cover/withdraw/random")
    async def loan_broker_cover_withdraw_endpoint(w: Workload = Depends(get_workload)):
        try:
            return await loan_broker_cover_withdraw(w.accounts, w.loan_brokers, w.client)
        except XRPLException as e:
            logger.warning(f"loan_broker_cover_withdraw: {type(e).__name__}: {e}")
        except Exception as e:
            logger.error(f"loan_broker_cover_withdraw failed: {type(e).__name__}: {e}")
            unreachable("workload::endpoint_exception", {"endpoint": "/loan/broker/cover/withdraw/random", "error": f"{type(e).__name__}: {e}"})

    @app.get("/loan/set/random")
    async def loan_set_endpoint(w: Workload = Depends(get_workload)):
        try:
            return await loan_set(w.accounts, w.loan_brokers, w.loans, w.client)
        except XRPLException as e:
            logger.warning(f"loan_set: {type(e).__name__}: {e}")
        except Exception as e:
            logger.error(f"loan_set failed: {type(e).__name__}: {e}")
            unreachable("workload::endpoint_exception", {"endpoint": "/loan/set/random", "error": f"{type(e).__name__}: {e}"})

    @app.get("/loan/delete/random")
    async def loan_delete_endpoint(w: Workload = Depends(get_workload)):
        try:
            return await loan_delete(w.accounts, w.loans, w.client)
        except XRPLException as e:
            logger.warning(f"loan_delete: {type(e).__name__}: {e}")
        except Exception as e:
            logger.error(f"loan_delete failed: {type(e).__name__}: {e}")
            unreachable("workload::endpoint_exception", {"endpoint": "/loan/delete/random", "error": f"{type(e).__name__}: {e}"})

    @app.get("/loan/manage/random")
    async def loan_manage_endpoint(w: Workload = Depends(get_workload)):
        try:
            return await loan_manage(w.accounts, w.loan_brokers, w.loans, w.client)
        except XRPLException as e:
            logger.warning(f"loan_manage: {type(e).__name__}: {e}")
        except Exception as e:
            logger.error(f"loan_manage failed: {type(e).__name__}: {e}")
            unreachable("workload::endpoint_exception", {"endpoint": "/loan/manage/random", "error": f"{type(e).__name__}: {e}"})

    @app.get("/loan/pay/random")
    async def loan_pay_endpoint(w: Workload = Depends(get_workload)):
        try:
            return await loan_pay(w.accounts, w.loans, w.client)
        except XRPLException as e:
            logger.warning(f"loan_pay: {type(e).__name__}: {e}")
        except Exception as e:
            logger.error(f"loan_pay failed: {type(e).__name__}: {e}")
            unreachable("workload::endpoint_exception", {"endpoint": "/loan/pay/random", "error": f"{type(e).__name__}: {e}"})

    return app

def main():
    logger.info("Loaded config from %s", config_file)
    conf = conf_file["workload"]
    logger.info("Config %s", json.dumps(conf, indent=2))
    workload = Workload(conf)
    app = create_app(workload)
    uvicorn.run(app, host="0.0.0.0", port=8000)

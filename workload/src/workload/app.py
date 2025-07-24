import json
import os
import pathlib
import time
from antithesis import lifecycle
from typing import Any
from workload.check_rippled_sync_state import is_rippled_synced # TODO:git use rippled_sync.py
from workload.create import create_accounts
from xrpl.models import IssuedCurrency
import asyncio
import httpx
from asyncio import TaskGroup
from xrpl.asyncio.account import get_next_valid_seq_number
from xrpl.asyncio.clients import AsyncJsonRpcClient
from xrpl.asyncio.ledger import get_latest_validated_ledger_sequence
from xrpl.core.binarycodec import encode_for_signing, encode
from xrpl.core.keypairs import sign
from xrpl.models.transactions import Payment
from xrpl.constants import CryptoAlgorithm
from xrpl.wallet import Wallet
from xrpl.account import does_account_exist
from xrpl.asyncio.clients import AsyncJsonRpcClient
from xrpl.asyncio.transaction.reliable_submission import XRPLReliableSubmissionException
import sys
import xrpl
import json
from xrpl.transaction import sign_and_submit, submit_and_wait
from workload.balances import get_account_tokens
from workload.config import conf_file, config_file
from workload.randoms import sample
from workload import utils, logger
from pathlib import Path
from fastapi import FastAPI, Depends
import uvicorn

app = FastAPI()

RIPPLED_URL = "http://172.17.0.4:5005"

rippled = AsyncJsonRpcClient("http://rippled:5005")


class Workload:
    def __init__(self, conf: dict[str, Any]):
        self.account_data = json.loads(Path("/accounts.json").read_text())
        print(json.dumps(self.account_data, indent=2))
        self.config = conf
        self.accounts = []
        self.gateways = []
        self.amms = []
        self.currencies = []
        self.counter = 0
        self.currency_codes = conf["currencies"]["codes"]
        self.start_time = time.time()
        rippled_host = os.environ.get("RIPPLED_NAME", conf["rippled"]["local"])
        rippled_rpc_port = os.environ.get("RIPPLED_RPC_PORT") or conf["rippled"]["json_rpc_port"]
        self.rippled = f"http://{rippled_host}:{rippled_rpc_port}"
        logger.info("Connecting to rippled at: %s", self.rippled)
        self.client = AsyncJsonRpcClient(self.rippled)
        xrpl.models.requests.Ledger()
        self.wait_for_network(self.rippled)
        utils.check_validator_proposing() or sys.exit("All validators not in 'proposing' state!")

        account_type = xrpl.models.requests.LedgerEntryType.ACCOUNT
        ledger_data_request = xrpl.models.requests.LedgerData(type=account_type)

        logger.info("%s after %ss", "Workload initialization complete", int(time.time() - self.start_time))
        logger.info("Workload going to sleep...")
        local_path = pathlib.Path(__file__).parents[3] / "tc/workload.json"
        workload_ready_msg =  "Workload initialization complete"
        lifecycle.setup_complete(details={"message": workload_ready_msg})
        logger.info("Called lifecycle setup_complete()")

    def configure_gateways(self, number: int, balance: str) -> None:
        """Configure the gateways for the network.

        Creates the accounts, enables default_ripple and creates some initial currencies.
        """
        logger.info("Configuring %s gateways", number)
        # BUG: Configuring 2 gateways causes error in xrpl-py
        gateway_config_start = time.time()
        gateway_wallets, responses = create_accounts(number=number, client=self.client, amount=balance)
        self.gateways = [Gateway(wallet) for idx, wallet in enumerate(gateway_wallets)]
        for gateway in self.gateways:
            logger.info("Setting up gateway %s", gateway.address)
            # Enable rippling on gateway's trustlines so tokens can be transferred
            accountset_txn = AccountSet(
                account=gateway.address,
                set_flag=AccountSetAsfFlag.ASF_DEFAULT_RIPPLE,
            )
            utils.wait_for_ledger_close(self.client)
            response = submit_and_wait(accountset_txn, self.client, gateway.wallet)

            for ic in utils.issue_currencies(gateway.address, self.currency_codes):
                gateway.issued_currencies[ic.currency] = ic
                self.currencies.append(ic)
        logger.info("%s gateways configured in %ss", len(self.gateways), int(time.time() - gateway_config_start))

    def configure_accounts(self, number: int, balance: str) -> None:
        # TODO: Too many variables, too complex
        trustset_limit = self.config["transactions"]["trustset"]["limit"]
        logger.info("Configuring %s accounts", number)
        account_create_start = time.time()
        wallets, responses = create_accounts(number=number, client=self.client, amount=balance)
        self.accounts = [UserAccount(wallet=wallet) for wallet in wallets]
        for account in self.accounts:
            does_account_exist(account.address, self.client)
        logger.debug("%s accounts created in %s ms", number, time.time() - account_create_start)
        utils.wait_for_ledger_close(self.client)
        accounts = self.accounts
        trustset_txns = []
        c = 1
        utils.wait_for_ledger_close(self.client)
        for gateway in self.gateways:
            for ic in gateway.issued_currencies.values():
                for account in accounts:
                    trustset_txns.append((account, TrustSet(account=account.address, limit_amount=ic.to_amount(value=trustset_limit))))
                    c += 1
        trustset_responses = []
        for account, txn in trustset_txns:
            trustset_responses.append(sign_and_submit(txn, self.client, account.wallet))
        # Simulate the accounts have bought tokens from the gateways
        payments = []
        for gateway in self.gateways:
            for account in accounts:
                for ic in gateway.issued_currencies.values():
                    # TODO: Get rid of magic numbers
                    usd_deposit = 10e3
                    rate = self.config["currencies"]["rate"][ic.currency]
                    token_disbursement = str(round(usd_deposit * 1e6 / int(rate), 10))
                    payment_amount = ic.to_amount(value=token_disbursement)
                    logger.info("Account %s depositing %s fiat USD for %s",
                                account.address, usd_deposit, utils.format_currency(payment_amount)
                    )
                    payments.append((gateway, Payment(account=gateway.address,
                                             amount=payment_amount,
                                             destination=account.address)))
        payment_responses = []
        for gw, txn in payments:
            payment_responses.append(sign_and_submit(txn, self.client, gw.wallet))
        for idx, account in enumerate(self.accounts):
            self.accounts[idx].balances = get_account_tokens(self.accounts[idx], self.client)["held"] # TODO: Fix

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
        if Workload.verbose:
            for c in issued_currencies:
                logger.debug(c)
        return issued_currencies


    def wait_for_network(self, rippled) -> None:
        timeout = self.config["rippled"]["timeout"]  # Wait at most 10 minutes
        wait_start = time.time()
        logger.debug("Waiting %ss for rippled at %s to be running.", timeout, rippled)
        while not (is_rippled_synced(rippled)):
            irs = is_rippled_synced(rippled)
            logger.info(f"is_rippled_synced returning: {irs}")

            if (rippled_ready_time := int(time.time() - self.start_time)) > timeout:
                logger.info("rippled ready after %ss", rippled_ready_time)
            logger.info("Waited %ss so far", int(time.time() - wait_start))
            wait_time = 10
            time.sleep(wait_time)
        logger.info("rippled ready...")

    async def submit_payments(self, n: int, wallet: Wallet, destination_address: str):
        seq = await get_next_valid_seq_number(wallet.address, client=rippled)
        latest_ledger = await get_latest_validated_ledger_sequence(client=rippled)

        signed_blobs = []
        for i in range(n):
            tx_json = Payment(
                account=wallet.address,
                amount="1000000",
                destination=destination_address,
                sequence=seq + i,
                fee="500",
                last_ledger_sequence=latest_ledger + 10,
                signing_pub_key=wallet.public_key
            ).to_xrpl()

            signing_blob = encode_for_signing(tx_json)
            signature = sign(signing_blob, wallet.private_key)
            tx_json["TxnSignature"] = signature

            signed_blob = encode(tx_json)
            signed_blobs.append(signed_blob)

        responses = []
        async with TaskGroup() as tg:
            for blob in signed_blobs:
                tg.create_task(self.submit_via_http(blob, responses))

        return responses

    async def submit_via_http(self, blob: str, responses: list):
        payload = {
            "method": "submit",
            "params": [{"tx_blob": blob}]
        }
        async with httpx.AsyncClient() as client:
            try:
                resp = await client.post(RIPPLED_URL, json=payload)
                responses.append(resp.json())
            except Exception as e:
                responses.append({"error": str(e)})

    async def pay(self):
        src, dst = sample(self.account_data, 2)
        src_secret = src[1]
        dst_address = dst[0]
        src_wallet = Wallet.from_secret(src_secret, algorithm=CryptoAlgorithm.SECP256K1)
        responses = await self.submit_payments(100, src_wallet, dst_address)
        for i in responses:
            print(i)
        return {"cool": "beans"}

def create_app(workload: Workload) -> FastAPI:
    app = FastAPI()

    # Dependency to inject the workload instance
    def get_workload():
        return workload

    # @app.get("/increment")
    # def increment_endpoint(w: Workload = Depends(get_workload)):
    #     return {"name": w.name, "count": w.increment()}
    @app.get("/increment")
    def increment_endpoint(w: Workload = Depends(get_workload)):
        return {"count": w.increment()}

    @app.get("/pay")
    async def make_payment(w: Workload = Depends(get_workload)):
        return await w.pay()

    return app

def main():
    logger.info("Loaded config from %s", config_file)
    conf = conf_file["workload"]
    logger.info("Config %s", json.dumps(conf, indent=2))
    # Create and configure your workload instance
    workload = Workload(conf)

    # Build the FastAPI app, injecting workload
    app = create_app(workload)

    # Run the server
    uvicorn.run(app, host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()

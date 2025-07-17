import json
import os
import pathlib
import time
from antithesis import lifecycle
from typing import Any
from workload.check_rippled_sync_state import is_rippled_synced # TODO:git use rippled_sync.py
from workload.create import create_accounts
from workload.models import Gateway, UserAccount, Amm
from xrpl.account import does_account_exist
from xrpl.clients import JsonRpcClient
from xrpl.models.currencies import IssuedCurrency
from xrpl.models.transactions import (
    AccountSet,
    AccountSetAsfFlag,
    AMMCreate,
    Payment,
    TrustSet,
)
from xrpl.asyncio.transaction.reliable_submission import XRPLReliableSubmissionException
import sys
from xrpl.models.requests import AMMInfo
from xrpl.models.currencies import XRP, IssuedCurrency
from itertools import combinations
from workload.randoms import randint

from xrpl.transaction import sign_and_submit, submit_and_wait
from workload.balances import get_account_tokens
from workload.config import conf_file, config_file
from workload import utils, logger




class Workload:
    def __init__(self, conf: dict[str, Any]):
        self.config = conf
        self.accounts = []
        self.gateways = []
        self.amms = []
        self.currencies = []
        self.currency_codes = conf["currencies"]["codes"]
        self.start_time = time.time()
        rippled_host = os.environ.get("RIPPLED_NAME") or conf["rippled"]["local"]
        rippled_rpc_port = os.environ.get("RIPPLED_RPC_PORT") or conf["rippled"]["json_rpc_port"]
        self.rippled = f"http://{rippled_host}:{rippled_rpc_port}"
        logger.info("Connecting to rippled at: %s", self.rippled)
        self.client = JsonRpcClient(self.rippled) # TODO: Go back to asyncio

        self.wait_for_network(self.rippled)
        utils.check_validator_proposing() or sys.exit("All validators not in 'proposing' state!")

        # logger.info("Initializing workload")
        # default_gateway_balance = str(int(self.config["gateways"]["default_balance"]))
        # default_account_balance = str(int(self.config["accounts"]["default_balance"]))
        # TODO: fix try/except
        # try:
            # self.configure_gateways(number=self.config["gateways"]["number"], balance=default_gateway_balance)
        # except:
            # logger.exception("Failed to configure gateways")
            # utils.check_validator_proposing() or sys.exit(1)
        # try:
            # self.configure_accounts(number=self.config["accounts"]["number"], balance=default_account_balance)
        # except:
            # logger.exception("Failed to configure accounts")
            # utils.check_validator_proposing() or sys.exit(1)


        # trading_fee = randint(0, 1000) if len(self.amms) > 1 else self.config["amm"]["trading_fee"]
        ### First time around only gateways create AMMs
        # for gw in self.gateways:
        #     for asset_pool in list(combinations([*list(gw.issued_currencies.values()), XRP()], 2)):
        #         asset_1, asset_2 = asset_pool
        #         if xrp_asset := asset_1 if XRP() in asset_pool and asset_2 != XRP() else asset_2 if asset_2 == XRP() else None:
        #             token = asset_1 if asset_2 == XRP() else asset_1
        #             rate = float(self.config["currencies"]["rate"][token.currency])
        #         token_value = self.config["amm"]["default_amm_token_deposit"]
        #         amount = asset_1.to_amount(value=token_value)
        #         amount2 = asset_2.to_amount(value=token_value)
        #         try:
        #             logger.info("%s creating amm of %s %s", gw.address, amount, amount2)
        #             response = submit_and_wait(AMMCreate(
        #                 account=gw.address,
        #                 amount=amount,
        #                 amount2=amount2,
        #                 trading_fee=trading_fee,
        #             ), self.client, gw.wallet)
        #             utils.wait_for_ledger_close(self.client)
        #             try:
        #                 amminfo = self.client.request(AMMInfo(asset=asset_1, asset2=asset_2)).result["amm"]
        #                 self.amms.append(
        #                     Amm(
        #                         account=amminfo["account"],
        #                         assets=[amminfo["amount"], amminfo["amount2"]],
        #                         lp_token=amminfo["lp_token"]))
        #                 logger.info("AMM %s ", self.amms[-1])
        #             except KeyError:
        #                 logger.error("AmmInfo() failed")

        #         except XRPLReliableSubmissionException as err:  # Probably Transaction failed: tecDUPLICATE
        #             logger.info("err %s", err)
        #             logger.info("AMM already exists for %s - %s", *asset_pool)


        logger.info("%s after %ss", "Workload initialization complete", int(time.time() - self.start_time))
        logger.info("Workload going to sleep...")
        # ### Dump the network data so test_composer directory can find it # TODO: get_workload_info()
        # docker_path = "/opt/antithesis/workload.json"
        # # local_path = pathlib.Path(pathlib.Path.cwd() / "config/volumes/tc/workload.json")
        local_path = pathlib.Path(__file__).parents[3] / "tc/workload.json"
        # path = docker_path if os.environ.get("RIPPLED_NAME") else local_path
        # logger.debug("Dumping workload info to path %s", path)

        # workload_data = {}
        # workload_data["accounts"] = [(a.address, a.wallet.seed) for a in self.accounts]
        # workload_data["gateway"] = [(a.address, a.wallet.seed) for a in self.gateways]
        # workload_data["issued_currencies"] = [(ic.currency, ic.issuer) for ic in self.currencies]
        # workload_data["currency_codes"] = self.currency_codes
        # with pathlib.Path(path).open("w", encoding="utf-8") as wl_data:
        #     json.dump(workload_data, wl_data, indent=2)
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


    def run(self) -> None:
        logger.debug("wl sleeping....")
        time.sleep(20)


def main() -> None:
    logger.info("Loaded config from %s", config_file)
    conf = conf_file["workload"]
    logger.info("Config %s", json.dumps(conf, indent=2))

    wl = Workload(conf)
    while True:
        wl.run()


if __name__ == "__main__":
    main()

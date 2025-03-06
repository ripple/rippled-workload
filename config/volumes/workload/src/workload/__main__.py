import json
import os
import pathlib
import time
from typing import Any
from workload.check_rippled_sync_state import is_rippled_synced
from workload.create import create_accounts
from workload.models import Gateway, UserAccount
from xrpl.account import does_account_exist, get_balance
from xrpl.clients import JsonRpcClient
from xrpl.ledger import get_latest_validated_ledger_sequence
from xrpl.models.currencies import IssuedCurrency
from xrpl.models.transactions import (
    AccountSet,
    AccountSetAsfFlag,
    Payment,
    TrustSet,
)
from xrpl.transaction import sign_and_submit, submit_and_wait
from xrpl.wallet import Wallet
from workload.balances import get_account_tokens
from workload.config import conf_file
from workload import utils, logger

conf = conf_file["workload"]

def set_default_ripple(gateway: Wallet, client: JsonRpcClient):
    accountset_txn = AccountSet(
        account=gateway.address,
        set_flag=AccountSetAsfFlag.ASF_DEFAULT_RIPPLE,
    )
    return submit_and_wait(accountset_txn, client, gateway)


def distribute_token(gateway: Wallet, accounts: list[Wallet], currency_code: str, client: JsonRpcClient | None = None):

    trustset_tx_responses = []
    payment_txns = []
    payment_txn_responses = []
    for account in accounts:
        logger.info("Distributing %s to %s", currency_code, account.address)

    for idx, account in enumerate(accounts):
        balance = get_balance(account.address, client)
        logger.debug("%s: %s", account.address, balance)
        currency = IssuedCurrency(
                currency=currency_code,
                issuer=gateway.address,
            )
        trust_amount = currency.to_amount(value=conf["transactions"]["trustset"]["trust_limit"])
        payment_amount = currency.to_amount(value=conf["currencies"]["token_distribution"])

        trustset_tx = TrustSet(
            account=account.address,
            limit_amount=trust_amount,
        )
        logger.info("[%s/%s] Setting trust for %s to %s", idx + 1, len(accounts), account.address, trust_amount)
        trustset_tx_responses.append(sign_and_submit(trustset_tx, client, account))
        payment_txns.append(Payment(
            account=gateway.address,
            amount=payment_amount,
            destination=account.address,
            send_max=payment_amount,
        ))


def wait_for_ledger_close(client: JsonRpcClient) -> None:
    target_ledger = get_latest_validated_ledger_sequence(client) + 1
    while (get_latest_validated_ledger_sequence(client)) < target_ledger:
        time.sleep(2)


class Workload:
    def __init__(self, conf: dict[str, Any]):
        self.config = conf
        self.accounts = []
        self.gateways = []
        self.currencies = []
        self.currency_codes = conf["currencies"]["codes"]
        self.start_time = time.time()
        rippled_host = os.environ.get("RIPPLED_NAME") or conf["rippled"]["local"]
        rippled_rpc_port = os.environ.get("RIPPLED_RPC_PORT") or conf["rippled"]["json_rpc_port"]
        self.rippled = f"http://{rippled_host}:{rippled_rpc_port}"
        logger.info("Connecting to rippled at: %s", self.rippled)
        self.client = JsonRpcClient(self.rippled)
        self.wait_for_network(self.rippled)

        logger.info("Initializing workload")
        default_gateway_balance = str(int(self.config["gateways"]["default_balance"]))
        default_account_balance = str(int(self.config["accounts"]["default_balance"]))
        self.configure_gateways(number=self.config["gateways"]["number"], balance=default_gateway_balance)
        self.configure_accounts(number=self.config["accounts"]["number"], balance=default_account_balance)
        logger.info("%s after %ss", "Workload initialization complete", int(time.time() - self.start_time))
        docker_path = "/opt/antithesis/test/v1/workload.json"
        local_path = pathlib.Path(pathlib.Path.cwd() / "config/volumes/tc/workload.json")
        path = docker_path if os.environ.get("RIPPLED_NAME") else local_path
        workload_data = [(a.address, a.wallet.seed) for a in self.accounts]
        with pathlib.Path(path).open("w", encoding="UTF-8") as target:
            json.dump(workload_data, target)
        pass
        # else ... what? Where to put locally?

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
            accountset_txn = AccountSet(
                account=gateway.address,
                set_flag=AccountSetAsfFlag.ASF_DEFAULT_RIPPLE,
            )
            wait_for_ledger_close(self.client)

            for ic in utils.issue_currencies(gateway.address, self.currency_codes):
                gateway.issued_currencies[ic.currency] = ic
                self.currencies.append(ic)
        logger.info("%s gateways configured in %ss", len(self.gateways), int(time.time() - gateway_config_start))

    def configure_accounts(self, number: int, balance: str):
        # TODO: Too many variables, too complex
        trustset_limit = self.config["transactions"]["trustset"]["limit"]
        logger.info("Configuring %s accounts", number)
        account_create_start = time.time()
        wallets, responses = create_accounts(number=number, client=self.client, amount=balance)
        self.accounts = [UserAccount(wallet=wallet) for wallet in wallets]
        for account in self.accounts:
            does_account_exist(account.address, self.client)
        logger.debug("%s accounts created in %ss", number, int(time.time() - account_create_start))
        wait_for_ledger_close(self.client)
        accounts = self.accounts
        trustset_txns = []
        c = 1
        wait_for_ledger_close(self.client)
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
                    logger.debug("Simulating account %s depositing %s USD for %s",
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

    def last_validated_ledger(self):
        return int(get_latest_validated_ledger_sequence(self.client))

    def wait_for_network(self, rippled) -> None:
        timeout = self.config["rippled"]["timeout"]  # Wait at most 10 minutes
        wait_start = time.time()
        logger.debug("Waiting %ss for rippled at %s to be running.", timeout, rippled)
        while not (is_rippled_synced(rippled)):
            if (rippled_ready_time := int(time.time() - self.start_time)) > timeout:
                logger.info("rippled ready after %ss", rippled_ready_time)
            logger.info("Waited %ss so far", int(time.time() - wait_start))
            wait_time = 10
            # logger.info("Sleeping %s seconds to wait for rippled to synced", wait_time)  # TODO: Don't spam this
            time.sleep(wait_time)
        logger.info("rippled ready...")

    def add_more_after(self, add_after_n_ledgers): # TODO: Implement in test composer
        try:
            if ledgers_elapsed := self.ledgers.get("runs_completed"):
                ledgers_elapsed = ledgers_elapsed[-1] - self.ledgers.get("first_seen")
                if ledgers_elapsed > add_after_n_ledgers:
                    self.add_more_accounts()
                    logger.info("Workload has %s accounts now", len(self.accounts))
        except Exception as err:
            logger.error(err)

    def add_more_accounts(self, number_of_accounts: int): # TODO: Implement in test_composer
        max_to_add = 50
        number_of_accounts = round((self.ledgers["runs_completed"][-1] - self.ledgers["first_seen"]) / 10)
        number_of_accounts = max_to_add if number_of_accounts > max_to_add else number_of_accounts
        if not number_of_accounts:
            logger.warning(f"Calculated number of accounts to create was: {number_of_accounts}")
            return
        logger.info("Adding %s more accounts...", number_of_accounts)
        accounts_configured = self.configure_accounts(number_of_accounts)
        new_accounts = self.accounts[-number_of_accounts:]
        if not all(does_account_exist(a.address, self.client) for a in new_accounts):
            error_msg = "Couldn't create additional accounts"
            logger.error(error_msg)
        elif accounts_configured:
            logger.info("Added %s more accounts", len(new_accounts))
        else:
            logger.error("Couldn't configure more accounts")

    def run(self):
        logger.info("wl just sleeping....")
        time.sleep(20)


def main():
    logger.info("Instantiating workload...")
    wl = Workload(conf)
    while True:
        wl.run()


if __name__ == "__main__":
    main()

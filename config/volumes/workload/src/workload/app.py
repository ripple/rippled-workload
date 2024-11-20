import datetime
import json
import sys
import time
from typing import ClassVar
from urllib import request
from urllib.error import HTTPError, URLError
from xrpl.models.transactions import Payment

from xrpl.account import does_account_exist, get_balance, get_next_valid_seq_number
from xrpl.clients import JsonRpcClient
from xrpl.ledger import get_latest_validated_ledger_sequence
from xrpl.models.currencies import IssuedCurrency
from xrpl.transaction import submit_and_wait
from xrpl.utils import datetime_to_ripple_time
from xrpl.wallet import Wallet

from workload import log, offer
from workload.config import Config
from workload.create import (
    check_cash,
    create_n_accounts,
    get_checks,
    submit_check_create,
)
from workload.state import get_all_account_balances, print_all_balances
from workload.escrow import escrow_create_txn, escrow_finish_txn
from workload.nft import account_nfts, nftoken_mint
from workload.randoms import choice, randrange, sample, randint
from workload.utils import format_bid_ask

sys.tracebacklimit = 1

config = Config()
rippled_ip = config.rippled.get("ip")
json_rpc_port = config.rippled.get("json_rpc_port")
rippled = f"http://{rippled_ip}:{json_rpc_port}"
verbose = config.verbose
def server_info(server: str = rippled) -> dict[str, str]:
    req = request.Request(server, data=json.dumps({"method": "server_info"}).encode())
    try:
        data = json.loads(request.urlopen(req, timeout=2).read())
        result = data["result"]["info"]
    except (HTTPError, URLError, TimeoutError) as err:
        log.error(f"{server} not running")
        result = {"exception": str(err)}
    except Exception as err:
        result = {"exception": str(err)}
    return result

def rippled_not_ready(sleep: int = 10):
    log.info("Checking if rippled is ready")
    try:
        info = server_info()
    except Exception as err:
        pass
    if not info.get("exception"):
        keys = ["hostid", "build_version", "server_state", "complete_ledgers", "peers", "time", "uptime"]
        info = info if verbose else {key: info[key] for key in keys}
        log.info("server_info: %s", json.dumps(info, indent=2))
        complete_ledgers, server_state = info.get("complete_ledgers"), info.get("server_state")
        if not (rippled_ready := server_state == "full" and len(complete_ledgers.split("-")) > 1):
            log.info("rippled not ready yet. Sleeping %ss...", sleep)
            rippled_ready = False
    else:
        # wait_for_resync()
        rippled_ready = False
    if not rippled_ready:
        time.sleep(sleep)
    return not rippled_ready

def check_validators():
    num_validators = 6
    timeout = 10
    validators_closing = []
    def timed_out(t):
        if int(t - start) > timeout:
            log.error("Validator check took too long")

    for i in range(num_validators):
        ip = i + 3
        ip = f"172.23.0.{i + 3}"
        info = server_info(f"http://{ip}:5005")
        if info.get("exception"):
            log.error(info)
            log.error(f"Something wrong with validator {i}")
            return
        server_state = info.get("server_state")
        complete_ledgers = info.get("complete_ledgers")
        last_ledger = complete_ledgers.split("-")[-1]
        # progressing = False
        start = time.time()
        while not all(validators_closing) and not timed_out(time.time()):
            info = server_info(f"http://{ip}:5005")
            complete_ledgers = info.get("complete_ledgers")
            ledger = complete_ledgers.split("-")[-1]
            validators_closing.append(ledger > last_ledger)

        log.info(f"Validator {i} seems good")
        log.info(f"\tServer state: {server_state} Complete ledgers: {complete_ledgers}")
    if all(validators_closing):
        log.info("All validators are progressing")


class Workload:
    accounts: list[Wallet] = list()
    gateways: list[Wallet] = list()
    currencies: ClassVar[list[IssuedCurrency]] = list()
    transactions: list
    error: list
    client: JsonRpcClient
    ledgers: dict[str, int | None] = {"first_seen": None, "run_started": None, "last_seen": None, "runs_completed": []}
    iterations: int = 0
    verbose: bool = False  # TODO: Configure this externally?

    def __init__(self):
        self.start_time = time.time()
        self.client = JsonRpcClient(rippled)
        self.wait_for_network()
        self.transactions = config.transactions

        accounts_configured = False
        log.info("Initializing workload")

        try:
            self.configure_gateways()
            while not accounts_configured:
                accounts_configured = self.configure_accounts()
            print(config.init_msg)
            log.info("%s after %ss", config.init_msg, int(time.time() - self.start_time))

            self.ledgers["first_seen"] = self.last_validated_ledger()
        except Exception:
            log.exception("Workload initialization failed")

    def configure_gateways(self) -> None:
        """Configure the gateways to be used.

        Creates the accounts, enables default_ripple and assigns all currencies that can be used in the run
        """
        number_of_gateways = config.number_of_gateways
        log.info("Configuring %s gateways", number_of_gateways)
        gateway_config_start = time.time()
        self.gateways = create_n_accounts(number_of_gateways)
        time.sleep(1)
        for gateway in self.gateways:
            offer.set_default_ripple(gateway)
            self.currencies.append(Workload.issue_currencies(gateway.address))
        log.info("%s gateways configured in %ss", len(self.gateways), int(time.time() - gateway_config_start))

    def configure_accounts(self, number_of_accounts=config.number_of_accounts):
        currency_codes = config.currency_codes
        log.info("Configuring %s accounts", number_of_accounts)
        account_create_start = time.time()
        self.accounts.extend(create_n_accounts(number_of_accounts))
        log.info("%s accounts created in %ss", number_of_accounts, int(time.time() - account_create_start))
        gateway = choice(self.gateways)
        try:
            account_config_start = time.time()
            for token in currency_codes:
                offer.distribute_tokens(gateway, self.accounts, token, self.client)
        except Exception:
            log.exception("Configuring accounts failed")
            return False
        log.info("%s accounts configured in %ss", number_of_accounts, int(time.time() - account_config_start))
        return True

    @classmethod
    def issue_currencies(cls, issuer: str, currency_code: list[str] = config.currency_codes) -> list[IssuedCurrency]:
        """Use a fixed set of currency codes to create IssuedCurrencies for a specific gateway.

        Args:
            issuer (str): Account_id of the gateway for all currencies
            currency_code (list[str], optional): _description_. Defaults to config.currency_codes.

        Returns:
            list[IssuedCurrency]: List of IssuedCurrencies a gateway provides

        """
        issued_currencies = [IssuedCurrency.from_dict(dict(issuer=issuer, currency=cc)) for cc in currency_code]
        log.debug("Issued %s currencies", len(issued_currencies))
        if Workload.verbose:
            for c in issued_currencies:
                log.debug(c)
        return issued_currencies

    def add_gateway(self):
        pass  # TODO: Implement

    def add_account(self):
        pass  # TODO: Implement

    def last_validated_ledger(self):
        return int(get_latest_validated_ledger_sequence(self.client))

    def wait_for_network(self):
        timeout = config.rippled.get("timeout")
        log.info("Waiting %ss for rippled to be running.", timeout)
        wait_start = time.time()
        while rippled_not_ready():
            if (rippled_ready_time := int(time.time() - self.start_time)) > timeout:
                log.info("rippled ready after %ss", rippled_ready_time)
            log.info("Waited %ss so far", int(time.time() - wait_start))
        self.ledgers["first_seen"] = self.last_validated_ledger()
        log.debug("Network closing ledgers at %s", self.ledgers["first_seen"])

    def add_more_after(self, add_after_n_ledgers):
        try:
            if ledgers_elapsed := self.ledgers.get("runs_completed"):
                ledgers_elapsed = ledgers_elapsed[-1] - self.ledgers.get("first_seen")
                if ledgers_elapsed > add_after_n_ledgers:
                    self.add_more_accounts()
                    log.info("Workload has %s accounts now", len(self.accounts))
        except Exception as err:
            log.error(err)

    def add_more_accounts(self, number_of_accounts=config.additional_accounts_per_round):
        max_to_add = 50
        number_of_accounts = round((self.ledgers["runs_completed"][-1] - self.ledgers["first_seen"]) / 10)
        number_of_accounts = max_to_add if number_of_accounts > max_to_add else number_of_accounts
        if not number_of_accounts:
            log.warning(f"Calculated number of accounts to create was: {number_of_accounts}")
            return
        log.info("Adding %s more accounts...", number_of_accounts)
        accounts_configured = self.configure_accounts(number_of_accounts)
        new_accounts = self.accounts[-number_of_accounts:]
        if not all(does_account_exist(a.address, self.client) for a in new_accounts):
            error_msg = "Couldn't create additional accounts"
            log.error(error_msg)
        elif accounts_configured:
            log.info("Added %s more accounts", len(new_accounts))
        else:
            log.error("Couldn't configure more accounts")

    def run(self):
        responses = []
        try:
            while True:
                self.ledgers["run_started"] = self.last_validated_ledger()
                if "offers" in self.transactions:
                    start = time.time()
                    max_offers_to_create = 20
                    # number_of_accounts_offers = randrange(1, len(accounts))  # How many accounts to generate offers with
                    number_of_accounts_offers = len(self.accounts)  # Just use all
                    # number_of_offers = randrange(1, max_offers_to_create)  # How many offers per account
                    number_of_offers = 5
                    accounts_offers = sample(self.accounts, number_of_accounts_offers)  # The Accounts to create offers with
                    offer_transactions = offer.generate_offers(accounts_offers, number_of_offers)
                    log.info("Generated %s sets of offers", len(offer_transactions))
                    offer_responses = []
                    # offer_responses = [submit_and_wait(txn, self.client, account) for account, txns in offer_transactions.items() for txn in txns]
                    for account, txns in offer_transactions.items():
                        for txn in txns:
                            # pays, gets = format_currency(txn.taker_pays), format_currency(txn.taker_gets)
                            offer_str = format_bid_ask(txn)
                            offer_dict = {"account": account.address, **offer_str}
                            info_msg = f"Offer created\n {json.dumps(offer_dict, indent=2)}"
                            log.debug(txn) if self.verbose else log.info(info_msg)
                            offer_responses.append(submit_and_wait(txn, self.client, account))
                    responses.extend(offer_responses)
                    log.info("Offers run finished in %ss", int(time.time() - start))

                if "checks" in self.transactions:
                    start = time.time()
                    try:
                        for _ in range(number_of_checks := randrange(1, config.number_of_accounts)):
                            source, destination = sample(self.accounts, 2)
                            min_check = 1
                            max_check = get_balance(source.address, self.client)
                            send_max = str(randrange(min_check, max_check))
                            check_info = (source, destination, send_max)
                            create_check_response = submit_check_create(*check_info)
                            responses.append(create_check_response)
                            checks = get_checks(destination)
                            check_ids = [c["index"] for c in checks]
                            [check_id] = (
                                sample(check_ids, 1)
                                if len(check_ids) > 1
                                else check_ids
                            )
                            check_cash_response = check_cash(check_id, destination, amount=send_max)
                            responses.append(check_cash_response.result)
                            # if not check_cash_response.result.get("validated_ledger_index"):
                            #     log.error("Couldn't validate %s", check_info)
                            # else:
                            #     log.debug("Validated %s", check_info)
                            # source_balance_after = get_balance(source.address, self.client)
                            # destination_balance_after = get_balance(destination.address, self.client)
                            # log.debug("source_balance_before: %s", source_balance_before)
                            # log.debug("destination_balance_before: %s", destination_balance_before)
                            # log.debug("source_balance_after: %s", source_balance_after)
                            # log.debug("destination_balance_after: %s", destination_balance_after)
                    except ValueError:
                        log.error("No accounts to write check")
                    except Exception:
                        log.error("Failed to write check")
                    log.info("Checks run finished after %ss", int(time.time() - start))

                if "nfts" in self.transactions:
                    start = time.time()
                    try:
                        [minter] = sample(self.accounts, 1)
                        taxon = randrange(0, 101)
                        nftoken_mint_response = nftoken_mint(minter, taxon, self.client)
                        responses.extend(nftoken_mint_response.result)
                        nfts = account_nfts(minter, self.client)
                    except Exception:
                        log.error("Failed to mint NFTs!")
                    log.info("NFT run finished in %ss", int(time.time() - start))

                if "escrow" in self.transactions:
                    start = time.time()
                    source = choice(self.accounts)
                    max_wait = 60  # seconds
                    min_wait = 3  # seconds
                    wait = randrange(min_wait, max_wait)
                    wait_more = min_wait
                    finish_after = datetime_to_ripple_time(datetime.datetime.now(tz=datetime.UTC)) + wait
                    amount = str(1_000_000000)
                    ect = escrow_create_txn(source, source, amount, finish_after=finish_after)
                    ect_submit_response = submit_and_wait(ect, self.client, source)
                    sequence = ect_submit_response.result.get("tx_json").get("Sequence")
                    eft = escrow_finish_txn(source, source, sequence)
                    responses.append(ect_submit_response)
                    while (n := datetime_to_ripple_time(datetime.datetime.now(tz=datetime.UTC))) < finish_after + wait_more:
                        time.sleep(wait_more)
                        log.info("%s still before %s. Waiting...", n, finish_after)
                    try:
                        eft_submit_response = submit_and_wait(eft, self.client, source)
                        responses.append(eft_submit_response)
                    except Exception:
                        log.exception("EscrowFinish failed! Retrying.")
                        time.sleep(wait_more)
                        try:
                            eft_submit_response = submit_and_wait(eft, self.client, source)
                            log.warning("EscrowFinish succeeded 2nd time")
                        except Exception:
                            log.exception("EscrowFinish failed again!")
                            # xrpl.asyncio.transaction.reliable_submission.XRPLReliableSubmissionException: Transaction failed: tecNO_PERMISSION
                    log.info("Escrow run finished in %ss", int(time.time() - start))

                if "payments" in self.transactions:
                    number_of_payments = 3
                    for idx in range(number_of_payments):
                        log.info(f"Random payment [{idx + 1}/{number_of_payments}]")
                        source, destination = sample(self.accounts, 2)
                        minimum_payment = 1
                        source_balance = maximum_payment = get_balance(source.address, self.client)
                        seq = get_next_valid_seq_number(source.address, self.client)
                        # if xrp:=randint(0, 1):
                        if True:
                            amount = randint(minimum_payment, maximum_payment)
                            # send xrp
                            seq = get_next_valid_seq_number(source.address, self.client)
                            payment_txn = Payment(
                                account=source.address,
                                amount=str(amount),
                                destination=destination.address,
                                sequence=seq,
                            )
                        try:
                            payment_txn_response = submit_and_wait(payment_txn, self.client, source)
                            log.info(f"Payment from {source.address} to {destination.address} for {amount}")
                        except Exception as err:
                            log.error(f"Payment from {source} to {destination} failed!")

                        if payment_txn_response.status != "success":
                            print(payment_txn_response)

                    print_all_balances(self.accounts, self.client)

                self.iterations += 1
                ledgers_elapsed = self.last_validated_ledger() - self.ledgers["run_started"]
                run_time = int(time.time() - self.start_time)
                log.info("Run %s finished in %s ledgers after %ss", self.iterations, ledgers_elapsed, run_time)

                if not self.ledgers.get("runs_completed"):
                    self.ledgers["runs_completed"] = [self.last_validated_ledger()]
                else:
                    self.ledgers["runs_completed"].append(self.last_validated_ledger())

                self.add_more_after(config.add_accounts_after_n_ledgers)
                print(f"{len(self.accounts)=}")

        except ConnectionRefusedError as err:
            log.error("Couldn't reach server %s", err)
        except Exception as err:
            log.error(err)

def main():
    log.info("Instantiating workload...")
    wl = Workload()
    try:
        while True:
            wl.run()

    except Exception as err:
        log.exception("Workload failed")
        check_validators()


if __name__ == "__main__":
    main()

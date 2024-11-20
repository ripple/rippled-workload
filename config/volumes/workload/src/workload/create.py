import time
from random import SystemRandom

from xrpl.account import does_account_exist
from xrpl.clients import JsonRpcClient
from xrpl.constants import CryptoAlgorithm
from xrpl.core import keypairs
from xrpl.ledger import get_latest_validated_ledger_sequence
from xrpl.models.requests import AccountObjects, AccountObjectType, Tx
from xrpl.models.response import ResponseStatus
from xrpl.models.transactions import CheckCash, CheckCreate, Payment
from xrpl.transaction import autofill_and_sign, sign_and_submit, submit_and_wait
from xrpl.wallet import Wallet

from workload import logging

urand = SystemRandom()  # TODO: import from module
randrange = urand.randrange
sample = urand.sample

log = logging.getLogger(__name__)

rippled_json = "http://172.23.0.9:5005"

default_algo = CryptoAlgorithm.SECP256K1
seed = keypairs.generate_seed(algorithm=default_algo)
public, private = keypairs.derive_keypair(seed)
test_account = keypairs.derive_classic_address(public)

default_balance = str(100_000_000_000000)

wait_ledgers = 10

_SEND_MAX = str(1000)

genesis_account = {
    "account_id": "rHb9CJAWyB4rj91VRWn96DkukG4bwdtyTh",
    "master_seed": "snoPBrXtMeMyMHUVTgbuqAfg1SUTb",
}

funding_account = Wallet.from_seed(seed=genesis_account["master_seed"], algorithm=default_algo)
client = JsonRpcClient(rippled_json)


def generate_creds():
    return Wallet.from_seed(keypairs.generate_seed(algorithm=default_algo), algorithm=default_algo)


def fund_account(funding_account, client):
    wallet = generate_creds()

    payment_tx = Payment(
        account=funding_account.address,
        amount=default_balance,
        destination=wallet.address,
    )
    try:
        response = sign_and_submit(payment_tx, client, funding_account)
        if response.status ==  ResponseStatus.SUCCESS:
            return response.result["tx_json"].get("hash"), wallet
    except Exception:
        log.exception("Creating %s failed!", wallet.address)


def ledger_not_arrived(wait_until):
    return int(get_latest_validated_ledger_sequence(client)) < wait_until


def create_n_accounts(number_of_accounts):
    responses = []
    for i in range(number_of_accounts):
        responses.append(fund_account(funding_account, client))
        log.info("[%s/%s] accounts created", i + 1, number_of_accounts)
    # fund_account(funding_account, client)
    # time.sleep(3)
    accounts_created = []
    wait_until = int(get_latest_validated_ledger_sequence(client)) + wait_ledgers

    while ledger_not_arrived(wait_until) and (len(accounts_created) < number_of_accounts):
        for idx, (txn_hash, wallet) in enumerate(responses):
            account_exists = does_account_exist(wallet.address, client)
            tx_response = client.request(Tx(transaction=txn_hash))
            if account_exists and tx_response.result.get("validated"):
                log.debug("%s verified", wallet.address)
                responses.pop(idx)
                accounts_created.append(wallet)
        time.sleep(3)
    return accounts_created


def check_create_txn(source, destination, send_max):
    check_dict = {
        "destination": destination.address,
        "account": source.address,
        "send_max": send_max,
    }

    log.debug("Signing CheckCreate transaction: %s", check_dict)
    check_create_txn = CheckCreate.from_dict(check_dict)
    check_create_response = autofill_and_sign(check_create_txn, client, source)
    log.debug("Signed CheckCreate transaction: %s", check_create_response)

    return check_create_response


def submit_check_create(source, destination, send_max):
    signed_check_create_txn = check_create_txn(source, destination, send_max)
    check_create_response = submit_and_wait(
        transaction=signed_check_create_txn,
        client=client,
        wallet=source,
        )
    log.debug("check_create_response: %s", check_create_response)

    return check_create_response


def get_checks(account):
    acct_checks = AccountObjects(
        account=account.address,
        type=AccountObjectType.CHECK,
        ledger_index="validated",
    )
    response = client.request(acct_checks)
    return response.result["account_objects"]


def check_cash(check_id, destination, amount):
    check_info = {
        "account": destination.address,
        "check_id": check_id,
        "amount": amount,
    }
    try:
        check_create_response = sign_and_submit(CheckCash.from_dict(check_info),
                                                client=client,
                                                wallet=destination,
                                )
        cid = f"{check_id[:5]}...{check_id[-5:]}"
        vli = check_create_response.result.get("validated_ledger_index")
        log.debug("Check %s cashed in ledger %s for %s drops", cid, vli, amount)
    except Exception:
        log.error("Couldn't CheckCash() %s", check_info)
    return check_create_response

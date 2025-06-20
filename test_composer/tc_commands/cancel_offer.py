import asyncio
import json
import urllib.request
from itertools import permutations, starmap
import sys
import anyio
from client import (
    generate_wallet_from_seed,
    get_accounts,
    get_txns,
    get_currencies,
    get_issued_currencies,
    rippled,
    workload_json,
)
from xrpl.transaction import sign_and_submit
from xrpl.models.transactions import Payment
from xrpl.account import get_next_valid_seq_number

from xrpl.models.transactions import OfferCancel
from workload import logger
from workload.models import UserAccount
from workload.randoms import choice
from xrpl.asyncio.transaction import sign_and_submit
from xrpl.models.currencies import XRP, IssuedCurrency
from xrpl.models.transactions import NFTokenMint, NFTokenMintFlag
from xrpl.models.transactions.transaction import Memo
from workload.config import conf_file
from client import rippled_

url = f"http://{rippled_}:5005" # TODO: Get from env

cancel_offer_payload = {
    "TransactionType": "OfferCancel",
    "Account": "",
    "Sequence": 0,
}

def make_request(url: str, command: dict):
    payload = bytes(json.dumps(command), encoding="utf-8")
    return urllib.request.urlopen(url, data=payload).read()

txn_data = asyncio.run(get_txns())

offer_to_cancel = choice(txn_data)
target_account = offer_to_cancel["account"]
offer_sequence = offer_to_cancel["sequence"]

def load_accounts(account_data):
    return [UserAccount(wallet=generate_wallet_from_seed(seed=seed)) for acct, seed in account_data]

account_data = asyncio.run(get_accounts())
accounts = load_accounts(account_data)

for account in accounts:
    if account.address == target_account:
        canceller = account

async def cancel_offer(account, offer_sequence):
    offer_cancel_txn = OfferCancel(
        account=canceller.address,
        offer_sequence=offer_sequence,
    )
    return await sign_and_submit(offer_cancel_txn, rippled, canceller.wallet)

cancel_offer_response = asyncio.run(cancel_offer(canceller, offer_sequence))
logger.info(cancel_offer_response.result)

#!/usr/bin/env -S python3 -u

import asyncio
import json

from client import generate_wallet, rippled, workload_json

# from antithesis.assertions import always
from workload import logger
from workload.config import conf_file
from workload.create import genesis_wallet
from xrpl.asyncio.account import get_next_valid_seq_number
from xrpl.asyncio.transaction import sign_and_submit, submit_and_wait
from xrpl.models.currencies import XRP, IssuedCurrency
from xrpl.models.requests import AccountInfo
from xrpl.models.transactions import (
    Payment,
    TrustSet,
)
from xrpl.utils import drops_to_xrp

with open(workload_json, encoding="utf-8") as json_data:
    workload_data = json.load(json_data)

rippled_config = conf_file["workload"]["rippled"]
genesis_account = conf_file["workload"]["genesis_account"]


async def create_account():
    wallet = generate_wallet()
    sequence = await get_next_valid_seq_number(genesis_wallet.address, rippled)
    amount = int(drops_to_xrp(str(int(conf_file["workload"]["accounts"]["default_balance"]))))
    payment_txn = Payment(
                account=genesis_wallet.address,
                amount=XRP().to_amount(value=amount),
                destination=wallet.address,
                sequence=sequence,
            )
    response = await submit_and_wait(
        transaction=payment_txn,
        client=rippled,
        wallet=genesis_wallet,
        )
    logger.debug("create_account() result: %s ", response.result)
    return response.result, wallet


async def main():
    # Load the list of potential tokens from the workload.json
    issued_currencies = [IssuedCurrency(issuer=i, currency=c) for c, i in workload_data["issued_currencies"]]
    create_account_result, wallet = await create_account()
    logger.debug("create_account_result for [%s] %s", create_account_result, wallet.address)

    # Create some trustset txns for those accounts to submit
    trustset_txns = []
    for ic in issued_currencies:
        limit_amount = ic.to_amount(value=conf_file["workload"]["transactions"]["trustset"]["limit"])
        trustset_tx = TrustSet(
            account=wallet.address,
            limit_amount=limit_amount,
        )
        trustset_txns.append(trustset_tx)
    trustset_txn_responses = [await sign_and_submit(tst, rippled, wallet) for tst in trustset_txns]
    for response in trustset_txn_responses:
        logger.debug(response.result)
    return trustset_txn_responses


res = asyncio.run(main())

from random import SystemRandom
import json
from xrpl.account import get_balance, get_next_valid_seq_number
from xrpl.clients import JsonRpcClient
from xrpl.models.requests import AccountInfo, AccountLines, AccountObjects, AccountOffers, Tx
from xrpl.models.amounts import IssuedCurrencyAmount
from xrpl.models.currencies import IssuedCurrency
from xrpl.models.transactions import Payment, TrustSet, OfferCreate, OfferCreateFlag
from xrpl.transaction import submit_and_wait, sign_and_submit
from xrpl.asyncio.transaction.reliable_submission import XRPLReliableSubmissionException
from workload import logging
from xrpl.models.transactions import AccountSet, AccountSetAsfFlag

json_client = "http://172.23.0.9:5005"
jclient = JsonRpcClient(json_client)

urand = SystemRandom()
randrange = urand.randrange
sample = urand.sample

log = logging.getLogger(__name__)

def distribute_tokens_parallel(gateway, accounts):
    trustset_txn_responses = []
    payment_txn_responses = []
    trustset_txns = []
    payment_txns = []
    for account in accounts:
        balance = get_balance(account.address, jclient)
        log.info("%s: %s", account.address, balance)
        currency_code = "USD"
        currency = IssuedCurrency(
                currency=currency_code,
                issuer=gateway.address,
            )
        trust_limit = str(10_000_000_000) # USD
        trust_amount = currency.to_amount(value=trust_limit)
        payment = str(100_000)
        payment_amount = currency.to_amount(value=payment)

        trustset_tx = TrustSet(
            account=account.address,
            limit_amount=trust_amount,
            )
        log.info("Distributing %s to %s", payment, account.address )
        trustset_txns.append(trustset_tx)
        payment_tx = Payment(
            account=gateway.address,
            amount=payment_amount,
            destination=account.address,
            send_max=payment_amount,
        )
        payment_txns.append(payment_tx)

        trustset_txn_responses = [sign_and_submit(trustset_tx, jclient, account) for trustset_tx in trustset_txns]
        # Ensure all trustsets validated
        payment_txn_responses= [ sign_and_submit(payment_txn, jclient, account) for payment_txn in payment_txns]

    pass

def distribute_tokens(gateway, accounts):
    trustset_tx_responses = []
    payment_tx_responses = []


    accountset_txn = AccountSet(
        account=gateway.address,
        clear_flag=AccountSetAsfFlag.ASF_DEFAULT_RIPPLE,
    )
    submit_and_wait(accountset_txn, jclient, gateway)

    for account in accounts[:-1]:
        balance = get_balance(account.address, jclient)
        log.debug("%s: %s", account.address, balance)
        currency_code = "USD"
        currency = IssuedCurrency(
                currency=currency_code,
                issuer=gateway.address,
            )
        trust_limit = str(10_000_000_000) # USD
        trust_amount = currency.to_amount(value=trust_limit)
        payment = str(100_000)
        payment_amount = currency.to_amount(value=payment)

        trustset_tx = TrustSet(
            account=account.address,
            limit_amount=trust_amount,
            )
        log.debug("Distributing %s to %s", payment, account.address )
        trustset_tx_responses.append(submit_and_wait(trustset_tx, jclient, account))

        payment_tx = Payment(
            account=gateway.address,
            amount=payment_amount,
            destination=account.address,
            send_max=payment_amount,
        )
        payment_tx_responses.append(submit_and_wait(payment_tx, jclient, gateway))
    return gateway
    # payment_tx_response = await submit_and_wait(payment_tx, async_client, wallet_1)

def create_offer(account, exchange):
    # exchange = {
    #     "taker_gets": str(777_000000),
    #     "taker_pays": IssuedCurrencyAmount(
    #         currency="USD",
    #         issuer=account.address,
    #         value=str(666),
    #     ),
    # }
    offer_create_tx = OfferCreate(
        account=account.address,
        **exchange,
    )
    # offer_create_tx = OfferCreate(
    #     account=account.address,
    #     taker_gets=str(gets),
    #     taker_pays=IssuedCurrencyAmount(
    #         currency="USD",
    #         issuer=account.address,
    #         value=str(pays),
    #     ),
    # )
    try:
        account_lines_response = jclient.request(AccountLines(account=account.address))
        json.dumps(account_lines_response.result.get("lines"), indent=2)
        log.debug(account_lines_response)
        offer_create_tx_response = submit_and_wait(offer_create_tx, jclient, account)
        log.debug(offer_create_tx_response.result)
        tx_id = offer_create_tx_response.result.get("hash")
        tx_response = jclient.request(Tx(transaction=tx_id)).result.get("meta")
        for node in tx_response.get("AffectedNodes"):
            for k, v in node.items():
                log.debug("%s: %s", k, v)
        return tx_response.get("validated")

    except XRPLReliableSubmissionException:
        log.error("Offer failed!")
        log.error(offer_create_tx)

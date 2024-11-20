import json
import time

from xrpl.account import get_balance, get_next_valid_seq_number
from xrpl.asyncio.transaction.reliable_submission import XRPLReliableSubmissionException
from xrpl.clients import JsonRpcClient
from xrpl.ledger import get_latest_validated_ledger_sequence
from xrpl.models.amounts import IssuedCurrencyAmount
from xrpl.models.currencies import IssuedCurrency
from xrpl.models.requests import AccountLines, Tx
from xrpl.models.requests.book_offers import BookOffers
from xrpl.models.transactions import (
    AccountSet,
    AccountSetAsfFlag,
    OfferCreate,
    Payment,
    TrustSet,
)
from xrpl.transaction import autofill_and_sign, sign_and_submit, submit_and_wait
from xrpl.wallet import Wallet

from workload import logging
from workload.config import Config
from workload.create import create_n_accounts, ledger_not_arrived
from workload.orderbook import generate_prices
from workload.randoms import choice, randrange, sample
from workload.utils import format_currency

json_client = "http://172.23.0.9:5005"
client = JsonRpcClient(json_client)


log = logging.getLogger(__name__)

config = Config()

def use_default_client():
    rippled = config.rippled
    ip = rippled["ip"]
    port = rippled["port"]
    json_client = f"http://{ip}:{port}"
    client = JsonRpcClient(json_client)
    return client

currency_codes = config.currency_codes

def distribute_tokens_parallel(gateway, accounts):
    trustset_txn_responses = []
    payment_txn_responses = []
    trustset_txns = []
    payment_txns = []
    for account in accounts:
        balance = get_balance(account.address, client)
        log.info("%s: %s", account.address, balance)
        currency_code = "USD"
        currency = IssuedCurrency(
                currency=currency_code,
                issuer=gateway.address,
            )
        trust_limit = config.trust_limit
        trust_amount = currency.to_amount(value=trust_limit)
        payment = config.token_payment
        payment_amount = currency.to_amount(value=payment)

        trustset_tx = TrustSet(
            account=account.address,
            limit_amount=trust_amount,
            )
        log.info("Distributing %s to %s", payment, account.address)
        trustset_txns.append(trustset_tx)
        payment_tx = Payment(
            account=gateway.address,
            amount=payment_amount,
            destination=account.address,
            send_max=payment_amount,
        )
        payment_txns.append(payment_tx)

        trustset_txn_responses = [sign_and_submit(trustset_tx, client, account) for trustset_tx in trustset_txns]
        # Ensure all trustsets validated
        payment_txn_responses = [sign_and_submit(payment_txn, client, account) for payment_txn in payment_txns]

def batch_set_default_ripple(gateways: list[Wallet], client):
    ast = []
    for gateway in gateways:
        aaa = get_next_valid_seq_number(gateway.address, client)
        accountset_txn = AccountSet(
            account=gateway.address,
            set_flag=AccountSetAsfFlag.ASF_DEFAULT_RIPPLE,
        )
        ast.append(autofill_and_sign(accountset_txn, client, gateway))
    return ast

def set_default_ripple(gateway: Wallet):
    accountset_txn = AccountSet(
        account=gateway.address,
        set_flag=AccountSetAsfFlag.ASF_DEFAULT_RIPPLE,
    )
    return submit_and_wait(accountset_txn, client, gateway)

def distribute_tokens(gateway: Wallet, accounts: list[Wallet], currency_code: str, client: JsonRpcClient | None = None):

    client = use_default_client() if client is None else client

    trustset_tx_responses = []
    payment_txns = []
    payment_txn_responses = []
    for account in accounts:
        log.info("Distributing %s to %s", currency_code, account.address)

    for idx, account in enumerate(accounts):
        balance = get_balance(account.address, client)
        log.debug("%s: %s", account.address, balance)
        currency = IssuedCurrency(
                currency=currency_code,
                issuer=gateway.address,
            )
        trust_amount = currency.to_amount(value=config.trust_limit)
        payment_amount = currency.to_amount(value=config.token_distribution)

        trustset_tx = TrustSet(
            account=account.address,
            limit_amount=trust_amount,
        )
        log.info("[%s/%s] Setting trust for %s to %s", idx + 1, len(accounts), account.address, trust_amount)
        try:
            trustset_tx_responses.append(sign_and_submit(trustset_tx, client, account))
        except Exception:
            log.exception("Trustset for %s %s failed", trustset_tx, account)
        payment_txns.append(Payment(
            account=gateway.address,
            amount=payment_amount,
            destination=account.address,
            send_max=payment_amount,
        ))

    def confirm_transactions(txn_response_list, client):
        all_txns_confirmed = False
        wait_ledgers = 10
        wait_until = int(get_latest_validated_ledger_sequence(client)) + wait_ledgers
        num_txns = len(txn_response_list)
        while ledger_not_arrived(wait_until) and not all_txns_confirmed:
            time.sleep(2)
            for idx, txn_response in enumerate(txn_response_list):
                txn_hash = txn_response.result.get("tx_json").get("hash")
                tx_response = client.request(Tx(transaction=txn_hash))
                if tx_response.result.get("validated"):
                    log.info("Transaction [ %s ] verified", txn_hash)
                    txn_response_list.pop(idx)
            if len(txn_response_list) == 0:
                all_txns_confirmed = True
            else:
                log.info("[%s/%s] transactions verified", (num_txns - len(txn_response_list)), num_txns)
        log.info("All transactions verified successful!")
        return True

    confirm_transactions(trustset_tx_responses, client)
    log.info("Making payments...")
    for payment_txn in payment_txns:
        amount, source, destination = format_currency(payment_txn.amount), payment_txn.account, payment_txn.destination
        msg = f"{amount} to  {destination} from {source}"
        try:
            log.info("Paying %s", msg)
            payment_txn_responses.append(sign_and_submit(payment_txn, client, gateway))
        except Exception:
            log.exception("Couldn't send %s", msg)

    confirm_transactions(payment_txn_responses, client)
    pass
    # confirm accounts
    # accounts_created = []
    # wait_until = int(get_latest_validated_ledger_sequence(client)) + wait_ledgers
    # while ledger_not_arrived(wait_until) and (len(accounts_created) < number_of_accounts):
    #     for idx, (txn_hash, wallet) in enumerate(responses):
    #         account_exists = does_account_exist(wallet.address, client)
    #         tx_response = client.request(Tx(transaction=txn_hash))
    #         if account_exists and tx_response.result.get("validated"):
    #             log.debug("%s verified", wallet.address)
    #             responses.pop(idx)
    #             accounts_created.append(wallet)
    #     time.sleep(2)
    return gateway

def create_offer(account: Wallet, exchange):
    offer_create_tx = OfferCreate(
        account=account.address,
        **exchange,
    )

    try:
        account_lines_response = client.request(AccountLines(account=account.address))
        json.dumps(account_lines_response.result.get("lines"), indent=2)
        log.debug(account_lines_response)
        offer_create_tx_response = submit_and_wait(offer_create_tx, client, account)
        log.debug(offer_create_tx_response.result)
        tx_id = offer_create_tx_response.result.get("hash")
        tx_response = client.request(Tx(transaction=tx_id)).result.get("meta")
        for node in tx_response.get("AffectedNodes"):
            for k, v in node.items():
                log.debug("%s: %s", k, v)
        return tx_response.get("validated")

    except XRPLReliableSubmissionException:
        log.error("Offer failed!")
        log.error(offer_create_tx)

def get_account_tokens(account: Wallet) -> list[dict[str, str]] | list:
    json_client = "http://172.23.0.9:5005"
    client = JsonRpcClient(json_client)
    accounts_tokens = []
    try:
        account_lines_response = client.request(AccountLines(account=account.address))
        if al_result := account_lines_response.result:
            accounts_tokens = [IssuedCurrencyAmount.from_dict({"issuer": al["account"], "value": al["balance"], "currency": al["currency"]}) for al in al_result.get("lines")]
    except Exception as err:
        log.error(err)
    else:
        return accounts_tokens

def create_offer_transaction(
        account: Wallet,
        gets: IssuedCurrencyAmount,
        pays: IssuedCurrencyAmount,
    ):
    offer_create_txn = OfferCreate(
        account=account.address,
        taker_gets=gets,
        taker_pays=pays,
    )
    return offer_create_txn

def invert_offer_txn(account: Wallet, offer_create_txn: OfferCreate) -> OfferCreate:
    taker_pays = offer_create_txn.taker_gets
    taker_gets = offer_create_txn.taker_pays
    return OfferCreate(
        account=account.address,
        taker_gets=taker_gets,
        taker_pays=taker_pays,
    )

def generate_offers(accounts: list[Wallet], number_of_offers: int = 10) -> list[OfferCreate]:
    """Generate random offers from tokens an account holds.

    Args:
        number_of_offers (int): How many offers to create
        accounts (list[Wallet]): _Create offers for these accounts

    Returns:
        _type_: list[OfferCreate]

    """
    try:
        a = accounts.copy()
        log.info("Generating %s offers for each of", number_of_offers)
        for acc in a:
            print(acc.address)
        offers = {}
        while a:
            aa = a.pop()
            offers[aa] = []
            for _ in range(number_of_offers):
                bid_token, ask_token = sample(get_account_tokens(aa), 2)  # Only try to create offers from tokens the account holds
                bid_price, ask_price = generate_prices(2, std_dev=5)
                bid, ask = (
                    bid_token.to_amount(bid_price),
                    ask_token.to_amount(ask_price),
                )
                # print(f"{bid=}, {ask=}")  # TODO: log.debug()
                offer = OfferCreate(account=aa.address, taker_gets=bid, taker_pays=ask)
                log.debug("Created offer\n%s", offer)
                offers[aa].append(offer)
        # log.debug("Created offers: %s", offers)
        return offers
    except Exception as err:
        log.exception("Couldn't generate list of offers\n%s")

def create_offers(tokens):
    offers = []
    mid = 100
    spread = randrange(1, 21)
    for a in accounts:
        t1, t2 = sample(tokens, 2)
        offer = choice(range(mid - spread, mid + spread))
        bid = choice(range(mid - spread, mid + spread))
        gets, pays = t1.to_amount(bid), t2.to_amount(offer)
        offers.append((a, create_offer_transaction(a, gets, pays)))
    return offers

def add_trust(account, currency, client, limit_amount=None):
    limit_amount = currency.to_amount(value=limit_amount or config.trust_limit)
    trustset_tx = TrustSet(
        account=account.address,
        limit_amount=limit_amount,
    )
    return submit_and_wait(trustset_tx, client, account)

def add_token(gateway: Wallet,
              issued_currency: IssuedCurrency,
              destination: Wallet,
              limit_amount: IssuedCurrencyAmount = None,
              value: int = None,
    ) -> None:
    json_client = "http://172.23.0.9:5005"
    client = JsonRpcClient(json_client)

    add_trust(destination, issued_currency, client)
    payment_amount = issued_currency.to_amount(value or config.token_distribution)
    payment_tx = Payment(
        account=gateway.address,
        amount=payment_amount,
        destination=destination.address,
        send_max=payment_amount,
    )
    response = submit_and_wait(payment_tx, client, gateway)

def get_book(taker_gets_currency: IssuedCurrency, taker_pays_currency: IssuedCurrency, client):
    book_offers_dict = {
        "taker_gets": taker_gets_currency,
        "taker_pays": taker_pays_currency,
    }
    book_offers_response = client.request(BookOffers.from_dict(book_offers_dict))
    return book_offers_response.result["offers"]

def print_book(book_offers_response):
    orders = [{
                "gets": offer["TakerGets"],
                "pays": offer["TakerPays"],
                "account": offer["Account"],
                "quality": offer["quality"],
        } for offer in book_offers_response
    ]
    offer = IssuedCurrencyAmount.from_dict(book_offers_response[0]["TakerGets"]).to_currency()
    bid = IssuedCurrencyAmount.from_dict(book_offers_response[0]["TakerPays"]).to_currency()
    print(f"Offers of {format_currency(offer)} for {format_currency(bid)}")
    for o in (sorted_by_quality := sorted(orders, key=lambda d: float(d["quality"]))):
        a = o.get('account')
        print(f"{a[:5]}...{a[-5:]} offers {format_currency(o.get('pays'))} for {format_currency(o.get('gets'))} at {o.get('quality')}")
        print(json.dumps(o, indent=2))


if __name__ == "__main__":

    json_client = "http://172.23.0.9:5005"
    client = JsonRpcClient(json_client)

    [gateway] = create_n_accounts(1)
    accounts = create_n_accounts(2)

    for token in ["USD", "BTC"]:
        distribute_tokens(gateway, accounts, token, client)



    def create_offers(tokens):
        offers = []
        mid = 100
        spread = randrange(1, 21)
        for a in accounts:
            t1, t2 = sample(tokens, 2)
            offer = choice(range(mid - spread, mid + spread))
            bid = choice(range(mid - spread, mid + spread))
            gets, pays = t1.to_amount(bid), t2.to_amount(offer)
            offers.append((a, create_offer_transaction(a, gets, pays)))
        return offers

    def get_all_account_balances(accounts):
        return [get_account_tokens(account) for account in accounts]

    def print_all_account_balances(accounts):
        balances = get_all_account_balances(accounts)

        # xrp_balance = f"{drops_to_xrp(str(get_balance(accounts[0].address, client)))}"
        for balance in balances:
            print(f"\t{balance}")
            # print(f"\t{format_currency(balance)}")

    # offers = []

    # # print_all_account_balances(accounts, client)
    # while True:
    #     offers.extend(create_offers(tokens))
    #     offer_responses = [submit_and_wait(txn[1], client, txn[0]) for txn in offers]
    #     print(f"{len(offer_responses)}")
    #     print_all_account_balances(accounts)
    #     pass
    #     account = choice(accounts)
    #     token = choice(get_account_tokens(account))

    # ##### and we're done

    # gets, pays = gbp.to_amount(100), usd.to_amount(100)
    # gets, pays = gbp.to_amount(30), usd.to_amount(47)
    # # prepare_accounts(gateways, accounts)
    # offers = []
    # offers.append(offer_1 := (account_1, create_offer_transaction(account_1, gets, pays)))
    # offers.append(offer_2 := (account_1, create_offer_transaction(account_1, gets, pays)))

    # pays, gets = gets, pays
    # offers.append(offer_2 := (account_2, create_offer_transaction(account_2, gets, pays)))

    # pays = pays.to_amount("200")
    # offers.append(offer_2 := (account_2, create_offer_transaction(account_2, gets, pays)))

    # pays = pays.to_amount("50")
    # offers.append(offer_2 := (account_2, create_offer_transaction(account_2, gets, pays)))
    # # offer_2 = invert_offer_txn(account_2, offer_1)
    # # submit_and_wait(txn, client, account)
    # offer_responses = [submit_and_wait(txn[1], client, txn[0]) for txn in offers]
    # taker_pays_offers = pays.to_currency()
    # taker_gets_offers = gets.to_currency()
    # book_offers_dict = {
    #     "taker_gets": taker_gets_offers,
    #     "taker_pays": taker_pays_offers,
    # }
    # inv_book_offers_dict = {
    #     "taker_gets": taker_pays_offers,
    #     "taker_pays": taker_gets_offers,
    # }
    # book_offers_result = client.request(BookOffers.from_dict(book_offers_dict))
    # inv_book_offers_result = client.request(BookOffers.from_dict(inv_book_offers_dict))

    # accounts_tokens = {}

    # offer = taker_gets_offers
    # bid = taker_pays_offers
    # gets, pays = gbp.to_amount(30), usd.to_amount(47)
    # offers = get_book(taker_pays_offers, taker_gets_offers, client)
    # print_book(offers)

    # # quality = in/out gets/pays
    # discount = 0.1
    # quality = initial_quality = 1
    # initial_offer = 5
    # initial_bid = initial_offer * initial_quality
    # bid = initial_bid
    # offer = initial_offer
    # escalation = [
    #     (100, 100),
    #     (110, 90),
    #     (120, 80),
    #     (130, 70),
    #     (150, 50),
    #     (200, 30),
    #     (500, 20),
    # ]
    # for i in range(1):
    #     for offer, bid in escalation:
    #         increase = randrange(0, 50) / 100
    #         new_bid = bid * 1 + increase
    #         increase = randrange(0, 50) / 100
    #         new_offer = offer * 1 + increase

    #         gets, pays = gbp.to_amount(new_offer), usd.to_amount(new_bid)
    #         buy_txn = create_offer_transaction(account_1, gets, pays)
    #         submit_and_wait(buy_txn, client, account_1)

    #         pays, gets = gbp.to_amount(new_offer), usd.to_amount(new_bid)
    #         sell_txn = create_offer_transaction(account_2, gets, pays)
    #         submit_and_wait(sell_txn, client, account_2)
    #     pass
    #     for c in [gbp, usd]:
    #         for a in accounts:
    #             print(f"matches for {a} for {c}")
    #             find_match(accounts, c, a)


    print(f"*** {taker_pays_offers} - {taker_gets_offers} ******************")
    for i in get_book(taker_pays_offers, taker_gets_offers, client):
        print(f"Q: {float(i.get('quality')):<8.6g} pays: {i.get('TakerPays').get('value')} gets: {i.get('TakerGets').get('value')} by: {i.get('Account')}")
    print(f"*** {taker_gets_offers} - {taker_pays_offers} ******************")
    for i in get_book(taker_gets_offers, taker_pays_offers, client):
        print(f"Q: {float(i.get('quality')):<8.6g} pays: {i.get('TakerPays').get('value')} gets: {i.get('TakerGets').get('value')} by: {i.get('Account')}")

    # find_match(accounts, gbp, accounts[0])

    # pass

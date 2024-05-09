from __future__ import annotations

import json
import sys
import time
from random import SystemRandom
from urllib import request
from urllib.error import HTTPError, URLError

import httpcore
from xrpl.account import get_balance
from xrpl.clients import JsonRpcClient
from xrpl.ledger import get_latest_validated_ledger_sequence
from xrpl.models.amounts import IssuedCurrencyAmount
from xrpl.models.currencies import IssuedCurrency
from xrpl.models.requests import BookOffers
from xrpl.transaction import autofill_and_sign, submit_and_wait

from workload import logging
from workload.create import check_cash, check_create_txn, create_n_accounts, get_checks, submit_check_create
from workload.make_offer import create_offer, distribute_tokens
from workload.nft import account_nfts, nftoken_mint, nftoken_mint_txn

log = logging.getLogger(__name__)

urand = SystemRandom()
randrange = urand.randrange
sample = urand.sample

rippled = "http://172.23.0.9:5005"
client = JsonRpcClient(rippled)

NUMBER_OF_ACCOUNTS = 11
WAIT_TIME = 10

sys.tracebacklimit = 1

def server_info() -> dict[str, str]:
    req = request.Request(rippled, data=json.dumps({"method": "server_info"}).encode())
    try:
        data = json.loads(request.urlopen(req, timeout=2).read())
        result = data["result"].get("info")
    except (HTTPError, URLError, TimeoutError) as err:
        err.add_note("Couldn't connect to rippled.")
        result = {"exception": str(err)}
    except Exception as err:
        result = {"exception": str(err)}
    return result

def rippled_not_ready(wait_time: int = WAIT_TIME):
    log.debug("Checking if rippled ready...")
    info = server_info()
    if not info.get("exception"):
        log.debug("server_info: %s", info)
        complete_ledgers, state = [v for k, v in info.items() if k in {"complete_ledgers", "server_state"}]
        if not (rippled_ready := state == "full" and len(complete_ledgers.split("-")) > 1):
            log.debug("rippled not ready yet. Sleeping %ss...", wait_time)
        log.debug("rippled_ready: %s", rippled_ready)
    else:
        rippled_ready = False
    if not rippled_ready:
        time.sleep(wait_time)
    return not rippled_ready

def run():
    wl_start = time.time()
    try:
        while rippled_not_ready(WAIT_TIME):
            log.debug("Waiting %ss for rippled to be running.", WAIT_TIME)
        if (rippled_ready_time := int(time.time() - wl_start)) > WAIT_TIME:
            log.debug("rippled ready after %s", rippled_ready_time)
        print("Workload initialization complete")
        it = 1
        while True:
            log.info("Ledger [%s] iteration %s", start_ledger := get_latest_validated_ledger_sequence(client), it)
            it_start = time.time()
            try:
                if accounts := create_n_accounts(NUMBER_OF_ACCOUNTS):
                    if create_offers := True:
                        gateway = accounts[-1]
                        distribute_tokens(gateway, accounts)
                        currency_code = "USD"
                        xrp_value = 50_000000
                        usd_value = 100
                        currency = IssuedCurrency(
                            currency=currency_code,
                            issuer=gateway.address,
                        )
                        taker_pays_amount = IssuedCurrencyAmount(
                                currency="USD",
                                issuer=gateway.address,
                                value=str(usd_value),
                        )
                        taker_gets_amount = str(xrp_value)
                        offer = {
                            "taker_gets": taker_gets_amount,
                            "taker_pays": currency.to_amount(usd_value),
                        }
                        offer_create_tx_responses = []
                        sign = randrange(2)
                        value_ = randrange(11)
                        if sign:
                            value_ *= -1
                        t_gets = currency.to_amount(int(taker_pays_amount.value))
                        t_pays = offer.get("taker_gets")
                        offer_1 = {"taker_gets": t_gets, "taker_pays": t_pays}
                        offer_2 = {"taker_gets": currency.to_amount(int(t_gets.value) + 1), "taker_pays": t_pays}
                        offer_3 = {"taker_gets": currency.to_amount(int(t_gets.value) + value_), "taker_pays": t_pays}

                        try:
                            for idx, offer_tx in enumerate([offer, offer_1, offer_2, offer_3]):
                                offer_create_tx_responses.append(create_offer(accounts[idx], offer_tx))
                        except IndexError:
                            log.error("No accounts created!")
                        except:
                            log.error("OfferCreate failed")
                        xrp = {"currency": "XRP"}

                        book_offers_dict = {
                            "taker_gets": xrp,
                            "taker_pays": currency,
                        }
                        counter_offers_dict = {
                            "taker_gets": currency,
                            "taker_pays": xrp,
                        }

                        book_offers = BookOffers.from_dict(book_offers_dict)
                        book_offers_result = client.request(book_offers)
                        offers = book_offers_result.result.get("offers")

                        counter_offers_d = BookOffers.from_dict(counter_offers_dict)
                        counter_offers = client.request(counter_offers_d).result.get("offers")

                    if write_checks := True:
                        try:
                            for _ in range(number_of_checks := randrange(1, NUMBER_OF_ACCOUNTS)):
                                source, destination = sample(accounts, 2)
                                source_balance_before = get_balance(source.address, client)
                                destination_balance_before = get_balance(destination.address, client)
                                min_check = 1
                                max_check = get_balance(source.address, client)
                                send_max = str(randrange(min_check, max_check))
                                check_info = (source, destination, send_max)
                                create_ = submit_check_create(*check_info)
                                checks = get_checks(destination)
                                check_ids = [c["index"] for c in checks]
                                [check_id] = sample(check_ids, 1) if len(check_ids) > 1 else check_ids
                                check_cash_response = check_cash(check_id, destination, amount=send_max)
                                if not check_cash_response.result.get("validated_ledger_index"):
                                    log.error("Couldn't validate %s", check_info)
                                else:
                                    log.debug("Validated %s", check_info)
                                source_balance_after = get_balance(source.address, client)
                                destination_balance_after = get_balance(destination.address, client)
                                log.debug("source_balance_before: %s", source_balance_before)
                                log.debug("destination_balance_before: %s", destination_balance_before)
                                log.debug("source_balance_after: %s", source_balance_after)
                                log.debug("destination_balance_after: %s", destination_balance_after)
                        except ValueError:
                            log.error("No accounts to write check")
                        except Exception:
                            log.error("Failed to write check")

                    if mint_nft := True:
                        try:
                            [minter] = sample(accounts, 1)
                            taxon = randrange(0, 101)
                            nftoken_mint(minter, taxon, client)
                            nfts = account_nfts(minter, client)
                        except Exception:
                            log.error("Failed to mint NFTs!")

            except (httpcore.ConnectError, ConnectionRefusedError):
                log.info("rippled probably not running")
            except TypeError:
                log.error("Failed to create accounts.")
            elapsed = f"{(time.time() - it_start):.2f}"
            try:
                ledgers_elapsed = get_latest_validated_ledger_sequence(client) - start_ledger
            except Exception:
                log.error("Couldn't get_latest_validated_ledger!")
            if not accounts:
                log.error("No accounts created!?")
            else:
                log.info("Created %s accounts in %s ledgers after %s seconds.", len(accounts), ledgers_elapsed, elapsed)
            it += 1
    except Exception:
        log.error("Trying again...")

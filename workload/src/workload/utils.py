import requests
import json
import os
import sys
from xrpl.models.amounts import IssuedCurrencyAmount
from xrpl.models.currencies import IssuedCurrency
from xrpl.ledger import get_latest_validated_ledger_sequence
from xrpl.clients import JsonRpcClient

import time
from workload import logger

def short_address(address: str) -> str:
    length = 7
    return address[:length]
    # return f"{address[:length]}"
    # sep = "..."
    # return f"{address[:length]}{sep}{address[-length:]}"

def format_currency(currency: IssuedCurrency | IssuedCurrencyAmount | str | dict[str, str], short: bool = False) -> str:
    if isinstance(currency, str):  # TODO: Fix this
        xrp_balance = currency
        w, d = pieces if len(pieces := xrp_balance.split(".")) > 1 else (0, int(pieces[0]))
        if w := (float(d) // 1e6):  # It's at least 1 XRP
            if len(str(w)) > 2:
                xrp_balance = round(float(xrp_balance))
            elif len(str(w)) < 3:
                xrp_balance = f"{float(xrp_balance):.4g}"
            currency_str = f"{xrp_balance} XRP"
        else:
            currency_str = f"{d} drops"
    else:
        if isinstance(currency, dict):
            value = currency.get("value")
            issuer = currency.get("issuer")
            currency_str = currency.get("currency")
        elif type(currency) in {IssuedCurrency, IssuedCurrencyAmount}:
            value = currency.value if isinstance(currency, IssuedCurrencyAmount) else None
            issuer = currency.issuer
            currency_str = currency.currency
        try:
            issuer_str = short_address(issuer) if short else issuer
        finally:
            currency_str = f"{currency_str}.{issuer_str}"

        if value is not None:
            currency_str = f"{value} {currency_str}"

    return currency_str

def format_currency_short(currency: IssuedCurrency | IssuedCurrencyAmount | str | dict[str, str], short: bool = True) -> str:
    return format_currency(currency=currency, short=True)

def issue_currencies(issuer: str, currency_code: list[str]) -> list[IssuedCurrency]:
    """Use a fixed set of currency codes to create IssuedCurrencies for a specific gateway.

    Args:
        issuer (str): Account_id of the gateway for all currencies
        currency_code (list[str], optional): _description_. Defaults to config.currency_codes.

    Returns:
        list[IssuedCurrency]: List of IssuedCurrencies a gateway provides

    """
    # TODO: format_currency()
    logger.info("Issuing %s %s", issuer, currency_code)
    return [IssuedCurrency(issuer=issuer, currency=cc) for cc in currency_code]

def wait_for_ledger_close(client: JsonRpcClient) -> None:

    target_ledger = get_latest_validated_ledger_sequence(client) + 1
    while (last_ledger := get_latest_validated_ledger_sequence(client)) < target_ledger:
        logger.debug("At %s waiting for ledger %s", last_ledger, target_ledger)
        time.sleep(3)
    logger.debug("Arrived at %s", target_ledger)

def check_validator_proposing(val_to_check: int | None = None) -> bool:
    val_name = os.environ.get("VALIDATOR_NAME", "val")
    num_validators = int(os.environ.get("NUM_VALIDATORS", 5))
    requests_timeout = 5 # No way rippled shouldn't respond by then
    if val_to_check and val_to_check > num_validators:
        logger.error("Validator [%s] outside number of validators range [%s]", val_to_check, num_validators)
        return False
    def get_last_ledger(url: str) -> int:
        response = requests.post(url, json={"method": "ledger", "params": [{"ledger_index": "validated"}]})
        last_ledger = response.json()["result"]["ledger"]["ledger_index"]
        return int(last_ledger)
    if val_to_check is not None:
        val_range = range(val_to_check - 1, val_to_check)
    else:
        val_range = range(num_validators)
    validators_proposing = {}
    for v in val_range:
        vnum = v
        url = f"http://{val_name}{vnum}:5005" # TODO: Magic port
        try:
            response = requests.post(url, json={"method": "server_info"}, timeout=requests_timeout)
        except requests.exceptions.ConnectTimeout:
            logger.error("rippled didn't respond")
            validators_proposing[vnum] = False
        except requests.exceptions.ConnectionError:
            logger.error("No server seen at %s", url)
            sys.exit(1)
        logger.info("Checking validator %s", vnum)
        if not response.ok:
            logger.error("status code: %s", response.status_code)
            logger.error("response: %s", response.content)
            validators_proposing[vnum] = False
        else:
            result_json = response.json()
            server_info = result_json["result"]["info"]
            logger.debug("server_info: %s", json.dumps(server_info, indent=2))
            start = time.perf_counter()
            if server_info.get("server_state") == "proposing":
                last_ledger = get_last_ledger(url)
                # REVIEW: Is this any better than wait_for_ledger_close()?
                while (current_ledger := get_last_ledger(url)) <= last_ledger:
                    logger.debug("Waiting for ledger %s", last_ledger + 1)
                    logger.debug("Last ledger %s current ledger %s", last_ledger, current_ledger)
                    time.sleep(3)
                validators_proposing[vnum] = True
                logger.info("Validator %s is closing ledgers at %s.", vnum, current_ledger)
            else:
                logger.debug("Validator %s not proposing", vnum)
                validators_proposing[vnum] = False
    return all(validators_proposing.values())

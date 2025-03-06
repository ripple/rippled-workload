from __future__ import annotations

from typing import TYPE_CHECKING, Any

from xrpl.models.amounts import IssuedCurrencyAmount
from xrpl.models.requests import AccountLines

# from utils import format_currency
from workload import logger

if TYPE_CHECKING:
    from xrpl.clients import JsonRpcClient
    from xrpl.wallet import Wallet


def get_all_account_lines(account: Wallet, client: JsonRpcClient) -> list[dict[str, Any]]:
    account_lines_response = client.request(AccountLines(account=account.address))
    return account_lines_response.result.get("lines")


# def get_tokens_issued(account: Wallet, client: JsonRpcClient | None) -> list[dict[str, str]] | None:
#     client = client if client is not None else JsonRpcClient(default_client)


# def get_token_balance(account: Wallet, client: JsonRpcClient | None) -> list[dict[str, str]] | None:
#     client = client if client is not None else JsonRpcClient(default_client)


def get_account_tokens(account: Wallet, client: JsonRpcClient) -> list[dict[str, Any]]:
    accounts_tokens = {
        "issued": {},
        "held": {},
    }
    logger.info("Looking up %s's tokens...", account.address)
    all_account_lines = get_all_account_lines(account, client)
    for al in all_account_lines:
        currency = al["currency"]
        balance = al["balance"]
        if float(al["balance"]) < 0:
            balance = str(float(balance) * -1)
            issuer = account.address
            holder = al["account"]
            logger.info("%s has issued %s %s tokens to %s", issuer, balance, currency, holder)
            ica = IssuedCurrencyAmount.from_dict({"issuer": account.address, "value": balance, "currency": al["currency"]})
            if accounts_tokens["issued"].get(holder):
                accounts_tokens["issued"][holder].append(ica)
            else:
                accounts_tokens["issued"][holder] = [ica]
        else:
            issuer = al["account"]
            holder = account.address
            ica = IssuedCurrencyAmount.from_dict({"issuer": issuer, "value": balance, "currency": al["currency"]})
            if accounts_tokens["held"].get(issuer):
                accounts_tokens["held"][issuer].append(ica)
            else:
                accounts_tokens["held"][issuer] = [ica]
            logger.info("%s holds %s %s %s tokens", holder, balance, currency, issuer)
    return accounts_tokens

# def print_all_account_token_balances(accounts, client):
#     for account in accounts:
#         print(f"Account: {account.address}")
#         tokens = get_account_tokens(account.wallet, client)
#         for issuer in tokens["held"].values():
#             for token in issuer:
#                 print(format_currency(token))


# def get_amm_balance():

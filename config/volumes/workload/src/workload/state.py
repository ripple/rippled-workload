from xrpl.account import get_balance


def get_all_account_balances(accounts, client):
    balances = {}
    for account in accounts:
        bal = get_balance(account.address, client)
        balances[account.address] = bal
    return balances


def print_all_balances(accounts, client):
    balances = get_all_account_balances(accounts, client)
    for account, balance in balances.items():
        print(f"{account}: {balance}")

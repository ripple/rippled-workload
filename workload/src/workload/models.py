from __future__ import annotations
from dataclasses import dataclass, field
from xrpl.models.currencies import XRP, IssuedCurrency
from xrpl.wallet import Wallet

def short_address(address):
    return "..".join([address[:6], address[-5:]])
@dataclass
class Account:
    wallet: Wallet
    address: str = field(init=False)

    def __post_init__(self):
        self.address = self.wallet.address

    def get_currencies(self):
        pass  # TODO: return all currencies account holds (including XRP as currency)

    def update_balances(self):
        # account_2_token_balance = [t for t in account_2_held_tokens[amount.issuer] if t.currency == amount.currency]
        for c in self.currencies:
            print("checking...")
            # log.info("Checking %s balance of %s", self.address, c)

    def __str__(self) -> str:
        return short_address(self.address)
@dataclass
class Gateway(Account):
    issued_currencies: dict = field(default_factory=dict)

@dataclass
class UserAccount(Account):
    balances: dict = field(default_factory=dict)

@dataclass
class Amm:
    account: str
    assets: list[IssuedCurrency]
    lp_token: list[IssuedCurrency]

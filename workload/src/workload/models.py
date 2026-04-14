from __future__ import annotations

from dataclasses import dataclass, field

import xrpl.models
from xrpl.models.currencies import IssuedCurrency, MPTCurrency
from xrpl.wallet import Wallet


def short_address(address: str) -> str:
    return "..".join([address[:6], address[-5:]])


@dataclass
class NFT:
    owner: UserAccount
    nftoken_id: str


@dataclass
class NFTOffer:
    creator: str
    offer_id: str
    nftoken_id: str
    is_sell: bool


@dataclass
class Account:
    wallet: Wallet
    address: str = field(init=False)

    def __post_init__(self):
        self.address = self.wallet.address

    def __str__(self) -> str:
        return short_address(self.address)


@dataclass
class Gateway(Account):
    issued_currencies: dict = field(default_factory=dict)


@dataclass
class UserAccount(Account):
    balances: dict = field(default_factory=dict)
    _tickets: set = field(default_factory=set)
    _nfts: set = field(default_factory=set)

    @property
    def nfts(self) -> set:
        return self._nfts

    @nfts.setter
    def nfts(self, value: set) -> None:
        self._nfts = value

    @property
    def tickets(self) -> set:
        return self._tickets

    @tickets.setter
    def tickets(self, value: set) -> None:
        self._tickets = value


@dataclass
class Amm:
    account: str
    assets: list[IssuedCurrency]
    lp_token: list[IssuedCurrency]


@dataclass
class Credential:
    issuer: str
    subject: str
    credential_type: str


@dataclass
class Vault:
    owner: str
    vault_id: str
    asset: IssuedCurrency | MPTCurrency | xrpl.models.XRP | None = None
    balance: int = 0
    shareholders: set[str] = field(default_factory=set)


@dataclass
class PermissionedDomain:
    owner: str
    domain_id: str


@dataclass
class MPTokenIssuance:
    issuer: str
    mpt_issuance_id: str


@dataclass
class TrustLine:
    account_a: str
    account_b: str
    currency: str


@dataclass
class LoanBroker:
    owner: str
    loan_broker_id: str
    vault_id: str
    cover_balance: int = 0


@dataclass
class Loan:
    borrower: str
    loan_id: str
    loan_broker_id: str
    principal: int = 0
    is_defaulted: bool = False
    is_impaired: bool = False

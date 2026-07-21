from __future__ import annotations

from dataclasses import dataclass, field

import xrpl.models
from xrpl.models.currencies import IssuedCurrency, MPTCurrency
from xrpl.wallet import Wallet


def short_address(address: str) -> str:
    return "..".join([address[:6], address[-5:]])


@dataclass
class NFT:
    owner: str  # account address
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

    def __post_init__(self) -> None:
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
    # ElGamal keypair for Confidential MPT (XLS-0096); set during confidential setup.
    elgamal_private_key: str | None = None
    elgamal_public_key: str | None = None

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
class Delegate:
    source: str
    delegate_address: str
    permissions: list[str]  # TransactionType values e.g. ["Payment", "TrustSet"]


@dataclass
class DID:
    """DID ledger entry attached to an account (XLS-72)."""

    account: str


@dataclass
class AMM:
    account: str
    assets: list[IssuedCurrency | MPTCurrency | xrpl.models.XRP]
    lp_token: list[IssuedCurrency]


@dataclass
class Credential:
    issuer: str
    subject: str
    credential_type: str
    accepted: bool = False
    # Real ledger ID; needed as a SponsorshipTransfer ObjectID.
    credential_id: str | None = None


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
    # (issuer, credential_type) pairs; member = owner or holder of a matching accepted credential.
    accepted_credentials: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class MPTokenIssuance:
    issuer: str
    mpt_issuance_id: str
    can_trade: bool = False
    can_transfer: bool = False
    require_auth: bool = False
    locked: bool = False
    holders: set[str] = field(default_factory=set)


@dataclass
class ConfidentialHolder:
    """Plaintext mirror of a holder's encrypted balances; ``version`` tracks the ledger counter."""

    address: str
    spending_balance: int = 0
    inbox_balance: int = 0  # pending-merge
    version: int = 0


@dataclass
class ConfidentialMPTIssuance:
    """Confidential-transfer MPT issuance (XLS-0096); issuer keys decrypt holders for clawback."""

    issuer: str
    mpt_issuance_id: str
    issuer_privkey: str
    issuer_pubkey: str
    holders: dict[str, ConfidentialHolder] = field(default_factory=dict)


@dataclass
class TrustLine:
    account_a: str
    account_b: str
    currency: str
    # Real RippleState ledger ID; needed as a SponsorshipTransfer ObjectID.
    # account_a is treated as the "owner" side for sponsorship purposes.
    trust_line_id: str | None = None


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


@dataclass
class Escrow:
    owner: str
    destination: str
    sequence: int
    condition: str | None = None
    fulfillment: str | None = None
    finish_after: int | None = None
    cancel_after: int | None = None
    # Real ledger ID (keylet(owner, sequence)); needed as a SponsorshipTransfer ObjectID.
    escrow_id: str | None = None


@dataclass
class Check:
    check_id: str
    creator: str
    destination: str
    send_max: str  # drops for XRP


@dataclass
class PaymentChannel:
    channel_id: str
    source: str
    destination: str
    amount: str  # total XRP drops allocated
    settle_delay: int


@dataclass
class Oracle:
    """Price Oracle ledger entry (XLS-47), keyed by (account, document_id).
    provider/asset_class are immutable after create, so updates keep them."""

    account: str
    document_id: int
    provider: str
    asset_class: str


@dataclass
class Sponsorship:
    """Prefunded Sponsorship ledger entry (XLS-68), keyed by (sponsor, sponsee)."""

    sponsor: str
    sponsee: str
    fee_amount: int  # drops remaining (local estimate, decays)
    max_fee: int | None
    remaining_owner_count: int
    require_sign_for_fee: bool = False
    require_sign_for_reserve: bool = False

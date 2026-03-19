from enum import StrEnum
from typing import Final

genesis_account: Final = {
    "address": "rHb9CJAWyB4rj91VRWn96DkukG4bwdtyTh",
    "seed": "snoPBrXtMeMyMHUVTgbuqAfg1SUTb",
    "public_key": "...",
    "private_key": "...",
}

GENESIS = genesis_account

ACCOUNT_ZERO: Final = "rrrrrrrrrrrrrrrrrrrrrhoLvTp"


class TxType(StrEnum):
    ACCOUNT_SET = "AccountSet"
    AMM_CREATE = "AMMCreate"
    AMM_DEPOSIT = "AMMDeposit"
    AMM_WITHDRAW = "AMMWithdraw"
    BATCH = "Batch"
    MPTOKEN_ISSUANCE_CREATE = "MPTokenIssuanceCreate"
    MPTOKEN_ISSUANCE_SET = "MPTokenIssuanceSet"
    MPTOKEN_AUTHORIZE = "MPTokenAuthorize"
    MPTOKEN_ISSUANCE_DESTROY = "MPTokenIssuanceDestroy"
    NFTOKEN_MINT = "NFTokenMint"
    NFTOKEN_BURN = "NFTokenBurn"
    NFTOKEN_CREATE_OFFER = "NFTokenCreateOffer"
    NFTOKEN_CANCEL_OFFER = "NFTokenCancelOffer"
    NFTOKEN_ACCEPT_OFFER = "NFTokenAcceptOffer"
    OFFER_CREATE = "OfferCreate"
    OFFER_CANCEL = "OfferCancel"
    TICKET_CREATE = "TicketCreate"
    PAYMENT = "Payment"
    TRUSTSET = "TrustSet"
    # Credentials
    CREDENTIAL_CREATE = "CredentialCreate"
    CREDENTIAL_ACCEPT = "CredentialAccept"
    CREDENTIAL_DELETE = "CredentialDelete"
    # Permissioned Domains
    PERMISSIONED_DOMAIN_SET = "PermissionedDomainSet"
    PERMISSIONED_DOMAIN_DELETE = "PermissionedDomainDelete"
    # Delegation
    DELEGATE_SET = "DelegateSet"
    # Vaults
    VAULT_CREATE = "VaultCreate"
    VAULT_SET = "VaultSet"
    VAULT_DELETE = "VaultDelete"
    VAULT_DEPOSIT = "VaultDeposit"
    VAULT_WITHDRAW = "VaultWithdraw"
    VAULT_CLAWBACK = "VaultClawback"


class TxState(StrEnum):
    CREATED = "CREATED"
    SUBMITTED = "SUBMITTED"
    RETRYABLE = "RETRYABLE"
    VALIDATED = "VALIDATED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    FAILED_NET = "FAILED_NET"


DEFAULT_CREATE_AMOUNT = int(100 * 1e6)
MAX_CREATE_AMOUNT = int(100e6 * 1e6)  # alot?
HORIZON = 15  # Transactions expire if not validated within 15 ledgers (~45-60 seconds)
RPC_TIMEOUT = 2.0
SUBMIT_TIMEOUT = 20
LOCK_TIMEOUT = 2.0

TERMINAL_STATE: frozenset[TxState] = frozenset({TxState.VALIDATED, TxState.REJECTED, TxState.EXPIRED})
# FAILED_NET is intentionally excluded from TERMINAL_STATE: a timed-out submission may have
# reached rippled and could still be queued. The account stays locked until the tx is either
# seen validated on-chain or its LastLedgerSequence expires.
PENDING_STATES: frozenset[TxState] = frozenset(
    {TxState.CREATED, TxState.SUBMITTED, TxState.RETRYABLE, TxState.FAILED_NET}
)

__all__ = [
    "ACCOUNT_ZERO",
    "DEFAULT_CREATE_AMOUNT",
    "GENESIS",
    "HORIZON",
    "LOCK_TIMEOUT",
    "MAX_CREATE_AMOUNT",
    "RPC_TIMEOUT",
    "SUBMIT_TIMEOUT",
    "PENDING_STATES",
    "TERMINAL_STATE",
    "TxType",
    "TxState",
]

"""Role-bound signatures required by fixCleanup3_4_0."""

from xrpl.core import keypairs
from xrpl.core.binarycodec import encode_for_signing
from xrpl.models.transactions import LoanSet, SponsorSignature, Transaction
from xrpl.models.transactions.loan_set import CounterpartySignature
from xrpl.wallet import Wallet

_TX_SIGN_PREFIX = b"STX\0"
_COUNTERPARTY_SIGN_PREFIX = b"CPT\0"
_SPONSOR_SIGN_PREFIX = b"SPN\0"


def _sign(transaction: Transaction, wallet: Wallet, prefix: bytes) -> str:
    payload = bytes.fromhex(encode_for_signing(transaction.to_xrpl()))
    if not payload.startswith(_TX_SIGN_PREFIX):
        raise RuntimeError("unexpected transaction signing prefix")
    return keypairs.sign(prefix + payload[4:], wallet.private_key)


def sign_loan_set_by_counterparty(wallet: Wallet, transaction: LoanSet) -> LoanSet:
    return transaction.__replace__(
        counterparty_signature=CounterpartySignature(
            signing_pub_key=wallet.public_key,
            txn_signature=_sign(transaction, wallet, _COUNTERPARTY_SIGN_PREFIX),
        )
    )


def sign_as_sponsor(wallet: Wallet, transaction: Transaction) -> Transaction:
    return transaction.__replace__(
        sponsor_signature=SponsorSignature(
            signing_pub_key=wallet.public_key,
            txn_signature=_sign(transaction, wallet, _SPONSOR_SIGN_PREFIX),
        )
    )

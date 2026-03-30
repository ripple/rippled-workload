"""Batch transaction generators for the antithesis workload.

Inner transactions can be any type except Batch, Vault*, and Loan* operations
(per Batch::disabledTxTypes in Batch.h).

NOTE: When adding a new transaction type to the workload, consider adding it
to _build_inner() as a batch inner type if it's not in the disabled list.
"""

import xrpl.models
from workload import logging, params
from workload.randoms import sample, choice
from workload.submit import submit_tx
from xrpl.asyncio.account import get_next_valid_seq_number
from xrpl.models import Batch, BatchFlag, Payment, IssuedCurrencyAmount
from xrpl.models.transactions import (
    AccountSet, AccountSetAsfFlag,
    TrustSet, NFTokenMint, NFTokenMintFlag,
    CredentialCreate, TicketCreate, PermissionedDomainSet,
    MPTokenIssuanceCreate, DelegateSet,
)
from xrpl.models.transactions.transaction import Memo
from xrpl.models.transactions.deposit_preauth import Credential as XRPLCredential

log = logging.getLogger(__name__)

# TODO: ASF_AUTHORIZED_NFTOKEN_MINTER excluded — requires nftoken_minter field (AXRT-118)
_BATCH_SAFE_FLAGS = [
    f for f in AccountSetAsfFlag
    if f != AccountSetAsfFlag.ASF_AUTHORIZED_NFTOKEN_MINTER
]

_INNER_COMMON = dict(
    flags=xrpl.models.TransactionFlag.TF_INNER_BATCH_TXN,
    fee="0",
    signing_pub_key="",
)

# All currently implemented types that are allowed in batch.
# Excluded: Vault*, Loan* (in Batch::disabledTxTypes), Batch (cannot nest).
_INNER_TYPES = [
    "Payment",
    "AccountSet",
    "TrustSet",
    "NFTokenMint",
    "CredentialCreate",
    "MPTokenIssuanceCreate",
    "TicketCreate",
    "PermissionedDomainSet",
    "DelegateSet",
]


def _build_inner(src, dst, sequence):
    """Build a random inner transaction for the batch."""
    tx_type = choice(_INNER_TYPES)
    common = {**_INNER_COMMON, "account": src.address, "sequence": sequence}

    if tx_type == "Payment":
        return Payment(amount=params.batch_inner_amount(), destination=dst, **common)

    if tx_type == "AccountSet":
        return AccountSet(set_flag=choice(_BATCH_SAFE_FLAGS), **common)

    if tx_type == "TrustSet":
        return TrustSet(
            limit_amount=IssuedCurrencyAmount(
                currency=params.currency_code(), issuer=dst, value=params.trustline_limit(),
            ),
            **common,
        )

    if tx_type == "NFTokenMint":
        return NFTokenMint(
            nftoken_taxon=params.nft_taxon(),
            transfer_fee=params.nft_transfer_fee(),
            memos=[Memo(memo_data=params.nft_memo().encode("utf-8").hex())],
            **common,
        )

    if tx_type == "CredentialCreate":
        return CredentialCreate(
            subject=dst,
            credential_type=params.credential_type(),
            **common,
        )

    if tx_type == "MPTokenIssuanceCreate":
        return MPTokenIssuanceCreate(**common)

    if tx_type == "TicketCreate":
        return TicketCreate(ticket_count=params.ticket_count(), **common)

    if tx_type == "PermissionedDomainSet":
        return PermissionedDomainSet(
            accepted_credentials=[XRPLCredential(
                issuer=dst,
                credential_type=params.credential_type(),
            )],
            **common,
        )

    if tx_type == "DelegateSet":
        return DelegateSet(authorize=dst, **common)

    return Payment(amount=params.batch_inner_amount(), destination=dst, **common)


async def batch_random(accounts, client):
    if params.should_send_faulty():
        return await _batch_random_faulty(accounts, client)
    return await _batch_random_valid(accounts, client)


async def _batch_random_valid(accounts, client):
    src_address, dst = sample(list(accounts), 2)
    sequence = await get_next_valid_seq_number(src_address, client)
    src = accounts[src_address]
    num_txns = params.batch_size()

    inner_txns = [
        _build_inner(src, dst, sequence + idx + 1)
        for idx in range(num_txns)
    ]

    batch_txn = Batch(
        account=src.address,
        flags=choice(list(BatchFlag)),
        raw_transactions=inner_txns,
        sequence=sequence,
    )
    await submit_tx("Batch", batch_txn, client, src.wallet)


async def _batch_random_faulty(accounts, client):
    pass  # TODO: fault injection

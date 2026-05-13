"""Ticket transaction generators for the antithesis workload."""

from collections.abc import Callable

import xrpl.models
from xrpl.asyncio.clients import AsyncJsonRpcClient
from xrpl.models.amounts import IssuedCurrencyAmount
from xrpl.models.transactions import (
    AccountSet,
    CheckCreate,
    CredentialCreate,
    EscrowCreate,
    MPTokenIssuanceCreate,
    NFTokenMint,
    Payment,
    PermissionedDomainSet,
    SetRegularKey,
    SignerListSet,
    TrustSet,
)
from xrpl.models.transactions.deposit_preauth import Credential as XRPLCredential
from xrpl.models.transactions.mptoken_issuance_create import MPTokenIssuanceCreateFlag
from xrpl.models.transactions.nftoken_mint import NFTokenMintFlag
from xrpl.models.transactions.signer_list_set import SignerEntry
from xrpl.models.transactions.transaction import Memo

from workload import params
from workload.assertions import tx_submitted
from workload.models import UserAccount
from workload.randoms import choice
from workload.submit import submit_tx

# ── Ticket-eligible transaction builders ─────────────────────────────
# Each builder takes (dst_addr, common) and returns a Transaction.
# ``common`` already contains account, sequence=0, ticket_sequence=N.
# Only types that need NO pre-existing object IDs belong here.
# To add a new type: add one entry to this dict.

_TicketBuilder = Callable[[str, dict], xrpl.models.Transaction]

_TICKET_BUILDERS: dict[str, _TicketBuilder] = {
    "Payment": lambda dst, c: Payment(
        destination=dst,
        amount=params.payment_amount(),
        **c,
    ),
    # dst as credential issuer is intentional — exercises the domain with an
    # arbitrary issuer so ticket-derived keylets get the coverage we need.
    "PermissionedDomainSet": lambda dst, c: PermissionedDomainSet(
        accepted_credentials=[
            XRPLCredential(issuer=dst, credential_type=params.credential_type()),
        ],
        **c,
    ),
    "NFTokenMint": lambda dst, c: NFTokenMint(
        nftoken_taxon=params.nft_taxon(),
        transfer_fee=params.nft_transfer_fee(),
        flags=NFTokenMintFlag.TF_TRANSFERABLE,
        memos=[Memo(memo_data=params.nft_memo().encode("utf-8").hex())],
        **c,
    ),
    "CredentialCreate": lambda dst, c: CredentialCreate(
        subject=dst,
        credential_type=params.credential_type(),
        **c,
    ),
    "MPTokenIssuanceCreate": lambda dst, c: MPTokenIssuanceCreate(
        maximum_amount=params.mpt_maximum_amount(),
        mptoken_metadata=params.mpt_metadata(),
        flags=MPTokenIssuanceCreateFlag.TF_MPT_CAN_LOCK,
        **c,
    ),
    "AccountSet": lambda dst, c: AccountSet(**c),
    "TrustSet": lambda dst, c: TrustSet(
        limit_amount=IssuedCurrencyAmount(
            currency=params.currency_code(),
            issuer=dst,
            value=params.trustline_limit(),
        ),
        **c,
    ),
    "CheckCreate": lambda dst, c: CheckCreate(
        destination=dst,
        send_max=params.check_send_max(),
        **c,
    ),
    "EscrowCreate": lambda dst, c: EscrowCreate(
        amount=params.escrow_amount(),
        destination=dst,
        finish_after=params.escrow_finish_after(),
        **c,
    ),
    "SetRegularKey": lambda dst, c: SetRegularKey(
        regular_key=dst,
        **c,
    ),
    "SignerListSet": lambda dst, c: SignerListSet(
        signer_quorum=1,
        signer_entries=[SignerEntry(account=dst, signer_weight=1)],
        **c,
    ),
}

# Types explicitly excluded from ticket use.  Reasons:
#   "objects"  — requires pre-existing object IDs (vault_id, nft, offer, etc.)
#   "circular" — tickets creating/using tickets
#   "cosign"   — requires multi-party signing
#   "batch"    — outer batch manages inner sequences
# Adding a new REGISTRY type without a builder or exclusion triggers a
# startup warning so new types can't silently skip ticket coverage.
_TICKET_EXCLUDED: set[str] = {
    # circular — tickets creating/using tickets
    "TicketCreate",
    "TicketUse",
    # batch — outer batch manages inner sequences
    "Batch",
    # cosign — requires multi-party signing
    "LoanSet",
    # objects — requires pre-existing object IDs
    "NFTokenBurn",
    "NFTokenModify",
    "NFTokenCreateOffer",
    "NFTokenCancelOffer",
    "NFTokenAcceptOffer",
    "MPTokenAuthorize",
    "MPTokenIssuanceSet",
    "MPTokenIssuanceDestroy",
    "CredentialAccept",
    "CredentialDelete",
    "VaultCreate",
    "VaultDeposit",
    "VaultWithdraw",
    "VaultSet",
    "VaultDelete",
    "VaultClawback",
    "PermissionedDomainDelete",
    "DelegateSet",
    "LoanBrokerSet",
    "LoanBrokerDelete",
    "LoanBrokerCoverDeposit",
    "LoanBrokerCoverWithdraw",
    "LoanDelete",
    "LoanManage",
    "LoanPay",
    "CheckCash",
    "CheckCancel",
    "EscrowFinish",
    "EscrowCancel",
    "PaymentChannelFund",
    "PaymentChannelClaim",
    # PaymentChannelCreate needs public_key from the wallet, which the
    # (dst, common)-only builder signature can't reach.
    "PaymentChannelCreate",
    # Clawback requires the source to be an authorised issuer.
    "Clawback",
    "AccountDelete",
}


def check_ticket_coverage() -> None:
    """Assert at startup that every REGISTRY type is covered.

    Called during app init.  Compares REGISTRY against _TICKET_BUILDERS
    and _TICKET_EXCLUDED so new types can't silently skip ticket coverage.
    Missing types fire an ``unreachable`` assertion that surfaces in
    Antithesis triage.
    """
    from antithesis.assertions import unreachable

    from workload.transactions import TX_TYPES

    for name in TX_TYPES:
        if name in _TICKET_BUILDERS or name in _TICKET_EXCLUDED:
            continue
        unreachable(
            "workload::ticket_coverage_missing",
            {"tx_type": name},
        )


# ── Create ───────────────────────────────────────────────────────────


async def ticket_create(accounts: dict[str, UserAccount], client: AsyncJsonRpcClient) -> None:
    if params.should_send_faulty():
        return await _ticket_create_faulty(accounts, client)
    return await _ticket_create_valid(accounts, client)


async def _ticket_create_valid(
    accounts: dict[str, UserAccount], client: AsyncJsonRpcClient
) -> None:
    account_id = choice(list(accounts))
    account = accounts[account_id]
    ticket_count = params.ticket_count()
    txn = xrpl.models.TicketCreate(
        account=account.address,
        ticket_count=ticket_count,
    )
    await submit_tx("TicketCreate", txn, client, account.wallet)


async def _ticket_create_faulty(
    accounts: dict[str, UserAccount], client: AsyncJsonRpcClient
) -> None:
    pass  # TODO: fault injection


# ── Use ──────────────────────────────────────────────────────────────


async def ticket_use(accounts: dict[str, UserAccount], client: AsyncJsonRpcClient) -> None:
    if params.should_send_faulty():
        return await _ticket_use_faulty(accounts, client)
    return await _ticket_use_valid(accounts, client)


async def _ticket_use_valid(accounts: dict[str, UserAccount], client: AsyncJsonRpcClient) -> None:
    # Find an account with tickets
    accounts_with_tickets = [(addr, acc) for addr, acc in accounts.items() if acc.tickets]
    if not accounts_with_tickets:
        return
    src_addr, src = choice(accounts_with_tickets)
    ticket_sequence = choice(list(src.tickets))
    # Pick a destination that isn't the source
    other_accounts = [a for a in accounts if a != src_addr]
    if not other_accounts:
        return
    dst = choice(other_accounts)

    # Randomly pick a ticket-eligible transaction type
    tx_type = choice(list(_TICKET_BUILDERS))
    common = {"account": src.address, "sequence": 0, "ticket_sequence": ticket_sequence}
    txn = _TICKET_BUILDERS[tx_type](dst, common)

    # Remove ticket optimistically to avoid reuse by concurrent calls
    src.tickets.discard(ticket_sequence)
    # Hit the TicketUse reachability assertion separately — submit_tx uses
    # the inner type so success/failure track the real TransactionType.
    tx_submitted("TicketUse", txn)
    await submit_tx(tx_type, txn, client, src.wallet)


async def _ticket_use_faulty(accounts: dict[str, UserAccount], client: AsyncJsonRpcClient) -> None:
    pass  # TODO: fault injection

"""Ticket transaction generators for the antithesis workload."""

from collections.abc import Callable
from dataclasses import dataclass

import xrpl.models
from xrpl.asyncio.clients import AsyncJsonRpcClient
from xrpl.models.amounts import IssuedCurrencyAmount
from xrpl.models.transactions import (
    AccountSet,
    CheckCreate,
    CredentialCreate,
    DIDSet,
    EscrowCreate,
    MPTokenIssuanceCreate,
    NFTokenMint,
    OfferCreate,
    Payment,
    PermissionedDomainSet,
    SetRegularKey,
    SignerListSet,
    TrustSet,
)
from xrpl.models.transactions.deposit_preauth import Credential as XRPLCredential
from xrpl.models.transactions.mptoken_issuance_create import MPTokenIssuanceCreateFlag
from xrpl.models.transactions.nftoken_mint import NFTokenMintFlag
from xrpl.models.transactions.offer_create import OfferCreateFlag
from xrpl.models.transactions.signer_list_set import SignerEntry
from xrpl.models.transactions.transaction import Memo
from xrpl.wallet import Wallet

from workload import params
from workload.assertions import tx_submitted
from workload.fuzz import submit_fuzzed
from workload.models import AMM, Credential, PermissionedDomain, UserAccount
from workload.randoms import choice
from workload.submit import submit_raw, submit_tx
from workload.transactions.permissioned_dex import _amm_iou, _domain_members

# ── Ticket-eligible transaction builders ─────────────────────────────
# Each builder takes a TicketCtx and returns a Transaction or None (skip when
# preconditions unmet). Add a new type here or to _TICKET_EXCLUDED.


@dataclass
class TicketCtx:
    """Passed to a ticket builder; ``common`` carries sequence=0 + ticket_sequence."""

    src: UserAccount
    dst: str
    common: dict
    accounts: dict[str, UserAccount]
    domains: list[PermissionedDomain]
    credentials: list[Credential]
    amms: list[AMM]


_TicketBuilder = Callable[[TicketCtx], "xrpl.models.Transaction | None"]


def _ticket_domain_offer(ctx: TicketCtx, *, hybrid: bool) -> "xrpl.models.Transaction | None":
    if not ctx.domains:
        return None
    iou = _amm_iou(ctx.amms)
    if iou is None:
        return None
    # Prefer a domain src belongs to (offer rests) else any domain (tecNO_PERMISSION).
    member_domains = [
        d
        for d in ctx.domains
        if ctx.src.address in _domain_members(d, ctx.accounts, ctx.credentials)
    ]
    domain = choice(member_domains) if member_domains else choice(ctx.domains)
    flags = int(OfferCreateFlag.TF_HYBRID) if hybrid else 0
    return OfferCreate(
        taker_gets=params.offer_xrp_drops(),
        taker_pays=IssuedCurrencyAmount(
            currency=iou.currency, issuer=iou.issuer, value=params.offer_iou_value()
        ),
        domain_id=domain.domain_id,
        flags=flags,
        **ctx.common,
    )


def _ticket_domain_payment(
    ctx: TicketCtx, *, cross_currency: bool
) -> "xrpl.models.Transaction | None":
    # Both parties must be domain members or preclaim fails (tecNO_PERMISSION).
    member_domains = []
    for d in ctx.domains:
        members = _domain_members(d, ctx.accounts, ctx.credentials)
        if ctx.src.address in members:
            member_domains.append((d, members))
    if not member_domains:
        return None
    domain, members = choice(member_domains)
    co_members = [m for m in members if m != ctx.src.address]
    dst = choice(co_members) if co_members else ctx.dst
    if cross_currency:
        iou = _amm_iou(ctx.amms)
        if iou is None:
            return None
        return Payment(
            destination=dst,
            amount=IssuedCurrencyAmount(
                currency=iou.currency, issuer=iou.issuer, value=params.offer_iou_value()
            ),
            send_max=params.offer_xrp_drops(),
            domain_id=domain.domain_id,
            **ctx.common,
        )
    return Payment(
        destination=dst,
        amount=params.payment_amount(),
        domain_id=domain.domain_id,
        **ctx.common,
    )


_TICKET_BUILDERS: dict[str, _TicketBuilder] = {
    "Payment": lambda ctx: Payment(
        destination=ctx.dst,
        amount=params.payment_amount(),
        **ctx.common,
    ),
    # dst as credential issuer is intentional — arbitrary issuer covers ticket keylets.
    "PermissionedDomainSet": lambda ctx: PermissionedDomainSet(
        accepted_credentials=[
            XRPLCredential(issuer=ctx.dst, credential_type=params.credential_type()),
        ],
        **ctx.common,
    ),
    "NFTokenMint": lambda ctx: NFTokenMint(
        nftoken_taxon=params.nft_taxon(),
        transfer_fee=params.nft_transfer_fee(),
        flags=NFTokenMintFlag.TF_TRANSFERABLE,
        memos=[Memo(memo_data=params.nft_memo().encode("utf-8").hex())],
        **ctx.common,
    ),
    "CredentialCreate": lambda ctx: CredentialCreate(
        subject=ctx.dst,
        credential_type=params.credential_type(),
        **ctx.common,
    ),
    "MPTokenIssuanceCreate": lambda ctx: MPTokenIssuanceCreate(
        maximum_amount=params.mpt_maximum_amount(),
        mptoken_metadata=params.mpt_metadata(),
        flags=MPTokenIssuanceCreateFlag.TF_MPT_CAN_LOCK,
        **ctx.common,
    ),
    "AccountSet": lambda ctx: AccountSet(**ctx.common),
    "TrustSet": lambda ctx: TrustSet(
        limit_amount=IssuedCurrencyAmount(
            currency=params.currency_code(),
            issuer=ctx.dst,
            value=params.trustline_limit(),
        ),
        **ctx.common,
    ),
    "CheckCreate": lambda ctx: CheckCreate(
        destination=ctx.dst,
        send_max=params.check_send_max(),
        **ctx.common,
    ),
    "EscrowCreate": lambda ctx: EscrowCreate(
        amount=params.escrow_amount(),
        destination=ctx.dst,
        finish_after=params.escrow_finish_after(),
        **ctx.common,
    ),
    "SetRegularKey": lambda ctx: SetRegularKey(
        regular_key=ctx.dst,
        **ctx.common,
    ),
    "SignerListSet": lambda ctx: SignerListSet(
        signer_quorum=1,
        signer_entries=[SignerEntry(account=ctx.dst, signer_weight=1)],
        **ctx.common,
    ),
    "DIDSet": lambda ctx: DIDSet(uri=params.did_hex_field(), **ctx.common),
    # Permissioned DEX x tickets — state-aware (reads ctx.domains/amms).
    "OfferCreateDomain": lambda ctx: _ticket_domain_offer(ctx, hybrid=False),
    "OfferCreateHybrid": lambda ctx: _ticket_domain_offer(ctx, hybrid=True),
    "PaymentDomain": lambda ctx: _ticket_domain_payment(ctx, cross_currency=False),
    "PaymentDomainXC": lambda ctx: _ticket_domain_payment(ctx, cross_currency=True),
}

# Types the (dst, common)-only builder signature can't reach: objects (need
# pre-existing IDs), circular (tickets on tickets), cosign, batch.
_TICKET_EXCLUDED: set[str] = {
    "TicketCreate",
    "TicketUse",
    "Batch",
    "LoanSet",
    "NFTokenBurn",
    "NFTokenModify",
    "NFTokenCreateOffer",
    "NFTokenCancelOffer",
    "NFTokenAcceptOffer",
    "MPTokenAuthorize",
    "MPTokenIssuanceSet",
    "MPTokenIssuanceDestroy",
    # Confidential MPT — need ElGamal keys / proofs the ticket builder signature can't supply.
    "ConfidentialMPTMergeInbox",
    "ConfidentialMPTConvert",
    "ConfidentialMPTSend",
    "ConfidentialMPTConvertBack",
    "ConfidentialMPTClawback",
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
    "AMMCreate",
    "AMMDeposit",
    "AMMWithdraw",
    "AMMVote",
    "AMMBid",
    "AMMDelete",
    "OfferCreate",
    "OfferCancel",
    "OfferCreateMPT",
    "PaymentMPT",
    "PaymentChannelCreate",  # needs public_key from the wallet
    "Clawback",  # source must be an authorised issuer
    "AccountDelete",
    "DIDDelete",
}


def check_ticket_coverage() -> None:
    """Fire ``unreachable`` for any REGISTRY type missing from both builders and exclusions."""
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


def _ticket_create_base(
    accounts: dict[str, UserAccount],
) -> tuple[xrpl.models.TicketCreate, Wallet] | None:
    """Valid TicketCreate + wallet; shared by valid and fuzz."""
    if not accounts:
        return None
    account = accounts[choice(list(accounts))]
    txn = xrpl.models.TicketCreate(account=account.address, ticket_count=params.ticket_count())
    return txn, account.wallet


async def _ticket_create_valid(
    accounts: dict[str, UserAccount], client: AsyncJsonRpcClient
) -> None:
    built = _ticket_create_base(accounts)
    if built is None:
        return
    txn, wallet = built
    await submit_tx("TicketCreate", txn, client, wallet)


async def _ticket_create_faulty(
    accounts: dict[str, UserAccount], client: AsyncJsonRpcClient
) -> None:
    built = _ticket_create_base(accounts)
    if built is None:
        return
    base, wallet = built

    if choice(["zero_count", "fuzz"]) == "fuzz":
        await submit_fuzzed("TicketCreate", base, client, wallet)
        return

    # zero_count: TicketCount 0 -> temINVALID_COUNT (xrpl-py forbids it at construction).
    def _mutate(d: dict) -> None:
        d["TicketCount"] = 0

    await submit_raw("TicketCreate", base, client, wallet, _mutate)


# ── Use ──────────────────────────────────────────────────────────────


async def ticket_use(
    accounts: dict[str, UserAccount],
    domains: list[PermissionedDomain],
    credentials: list[Credential],
    amms: list[AMM],
    client: AsyncJsonRpcClient,
) -> None:
    if params.should_send_faulty():
        return await _ticket_use_faulty(accounts, domains, credentials, amms, client)
    return await _ticket_use_valid(accounts, domains, credentials, amms, client)


async def _ticket_use_valid(
    accounts: dict[str, UserAccount],
    domains: list[PermissionedDomain],
    credentials: list[Credential],
    amms: list[AMM],
    client: AsyncJsonRpcClient,
) -> None:
    accounts_with_tickets = [(addr, acc) for addr, acc in accounts.items() if acc.tickets]
    if not accounts_with_tickets:
        return
    src_addr, src = choice(accounts_with_tickets)
    ticket_sequence = choice(list(src.tickets))
    other_accounts = [a for a in accounts if a != src_addr]
    if not other_accounts:
        return
    dst = choice(other_accounts)

    tx_type = choice(list(_TICKET_BUILDERS))
    common = {"account": src.address, "sequence": 0, "ticket_sequence": ticket_sequence}
    ctx = TicketCtx(
        src=src,
        dst=dst,
        common=common,
        accounts=accounts,
        domains=domains,
        credentials=credentials,
        amms=amms,
    )
    txn = _TICKET_BUILDERS[tx_type](ctx)
    if txn is None:
        return  # state-aware builder couldn't satisfy preconditions this round

    # Optimistic discard avoids reuse by concurrent calls.
    src.tickets.discard(ticket_sequence)
    # Separate TicketUse assertion — submit_tx tracks success/failure on the inner type.
    tx_submitted("TicketUse", txn)
    await submit_tx(tx_type, txn, client, src.wallet)


async def _ticket_use_faulty(
    accounts: dict[str, UserAccount],
    domains: list[PermissionedDomain],
    credentials: list[Credential],
    amms: list[AMM],
    client: AsyncJsonRpcClient,
) -> None:
    accounts_with_tickets = [(addr, acc) for addr, acc in accounts.items() if acc.tickets]
    if not accounts_with_tickets:
        return
    src_addr, src = choice(accounts_with_tickets)
    ticket_sequence = choice(list(src.tickets))
    other_accounts = [a for a in accounts if a != src_addr]
    if not other_accounts:
        return
    dst = choice(other_accounts)
    common = {"account": src.address, "sequence": 0, "ticket_sequence": ticket_sequence}

    # Consumed on a tec apply; optimistic discard mirrors the valid path.
    src.tickets.discard(ticket_sequence)

    if choice(["overdraw", "fuzz"]) == "fuzz":
        ctx = TicketCtx(
            src=src,
            dst=dst,
            common=common,
            accounts=accounts,
            domains=domains,
            credentials=credentials,
            amms=amms,
        )
        base = _TICKET_BUILDERS[choice(list(_TICKET_BUILDERS))](ctx)
        if base is None:
            return
        await submit_fuzzed("TicketUse", base, client, src.wallet)
        return

    # overdraw: a ticketed Payment that overdraws -> tecUNFUNDED_PAYMENT. It validates with
    # TicketSequence set, so ws_listener attributes the failure to the TicketUse bucket.
    txn = Payment(destination=dst, amount="10000000000000", **common)
    tx_submitted("TicketUse", txn)
    await submit_tx("Payment", txn, client, src.wallet)

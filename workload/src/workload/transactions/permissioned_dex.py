"""Permissioned DEX workload handlers (featurePermissionedDEX).

Domain-restricted offers, hybrid offers, and domain-restricted payments. A
``DomainID`` offer crosses only the domain-keyed order book; a hybrid offer
additionally rests in the open book. Both require the submitting account to be
a member of the domain (owner, or holder of an accepted matching credential) —
otherwise rippled returns ``tecNO_PERMISSION``.

These are registered under synthetic assertion names (OfferCreateDomain,
OfferCreateHybrid, PaymentDomain, PaymentDomainXC); the on-ledger
TransactionType stays OfferCreate / Payment. ws_listener fires the matching
``tx_result`` so the success/failure buckets resolve (see the TicketUse
precedent in ws_listener.py).
"""

from __future__ import annotations

import xrpl.models
from xrpl.asyncio.clients import AsyncJsonRpcClient
from xrpl.models import IssuedCurrencyAmount as IOUAmount
from xrpl.models.currencies import IssuedCurrency
from xrpl.models.transactions import OfferCreate, Payment
from xrpl.models.transactions.offer_create import OfferCreateFlag

from workload import params
from workload.models import AMM, Credential, PermissionedDomain, UserAccount
from workload.randoms import choice, sample
from workload.submit import submit_tx

# ── Membership helpers ──────────────────────────────────────────────


def _domain_members(
    domain: PermissionedDomain,
    accounts: dict[str, UserAccount],
    credentials: list[Credential],
) -> list[str]:
    """Accounts that are members of ``domain``: the owner plus every account
    holding an accepted credential matching one of the domain's accepted
    (issuer, credential_type) pairs. Restricted to accounts we control."""
    members = {domain.owner}
    accepted = set(domain.accepted_credentials)
    for c in credentials:
        if c.accepted and (c.issuer, c.credential_type) in accepted and c.subject in accounts:
            members.add(c.subject)
    return [m for m in members if m in accounts]


def _pick_domain_with_members(
    domains: list[PermissionedDomain],
    accounts: dict[str, UserAccount],
    credentials: list[Credential],
    minimum: int,
) -> tuple[PermissionedDomain, list[str]] | None:
    """Pick a random domain (owned by an account we control) that has at least
    ``minimum`` members. Returns the domain and its member list, or None."""
    candidates = []
    for d in domains:
        if d.owner not in accounts:
            continue
        members = _domain_members(d, accounts, credentials)
        if len(members) >= minimum:
            candidates.append((d, members))
    return choice(candidates) if candidates else None


def _amm_iou(amms: list[AMM]) -> IssuedCurrency | None:
    """Pick a real gateway IOU from a random AMM's asset pair."""
    if not amms:
        return None
    amm = choice(amms)
    ious = [a for a in amm.assets if not isinstance(a, xrpl.models.XRP)]
    return choice(ious) if ious else None


# ── Domain / hybrid offers ──────────────────────────────────────────


async def offer_create_domain(
    accounts: dict[str, UserAccount],
    domains: list[PermissionedDomain],
    credentials: list[Credential],
    amms: list[AMM],
    client: AsyncJsonRpcClient,
) -> None:
    if params.should_send_faulty():
        return await _domain_offer_faulty(
            accounts, domains, credentials, amms, client, hybrid=False, name="OfferCreateDomain"
        )
    return await _domain_offer_valid(
        accounts, domains, credentials, amms, client, hybrid=False, name="OfferCreateDomain"
    )


async def offer_create_hybrid(
    accounts: dict[str, UserAccount],
    domains: list[PermissionedDomain],
    credentials: list[Credential],
    amms: list[AMM],
    client: AsyncJsonRpcClient,
) -> None:
    if params.should_send_faulty():
        return await _domain_offer_faulty(
            accounts, domains, credentials, amms, client, hybrid=True, name="OfferCreateHybrid"
        )
    return await _domain_offer_valid(
        accounts, domains, credentials, amms, client, hybrid=True, name="OfferCreateHybrid"
    )


async def _domain_offer_valid(
    accounts: dict[str, UserAccount],
    domains: list[PermissionedDomain],
    credentials: list[Credential],
    amms: list[AMM],
    client: AsyncJsonRpcClient,
    *,
    hybrid: bool,
    name: str,
) -> None:
    picked = _pick_domain_with_members(domains, accounts, credentials, minimum=1)
    if not picked:
        return
    iou = _amm_iou(amms)
    if iou is None:
        return
    domain, members = picked
    member = accounts[choice(members)]
    # taker_gets = XRP (the member always has it); taker_pays = a real gateway
    # IOU. The domain book starts empty, so the offer rests cleanly.
    taker_pays = IOUAmount(currency=iou.currency, issuer=iou.issuer, value=params.offer_iou_value())
    flags = int(OfferCreateFlag.TF_HYBRID) if hybrid else 0
    txn = OfferCreate(
        account=member.address,
        taker_gets=params.offer_xrp_drops(),
        taker_pays=taker_pays,
        domain_id=domain.domain_id,
        flags=flags,
    )
    await submit_tx(name, txn, client, member.wallet)


async def _domain_offer_faulty(
    accounts: dict[str, UserAccount],
    domains: list[PermissionedDomain],
    credentials: list[Credential],
    amms: list[AMM],
    client: AsyncJsonRpcClient,
    *,
    hybrid: bool,
    name: str,
) -> None:
    if not accounts:
        return
    iou = _amm_iou(amms)
    if iou is None:
        return
    taker_pays = IOUAmount(currency=iou.currency, issuer=iou.issuer, value=params.offer_iou_value())

    mutation = choice(["not_in_domain", "fake_domain"])
    if mutation == "not_in_domain":
        # Real domain, but submit from an account that is NOT a member.
        picked = _pick_domain_with_members(domains, accounts, credentials, minimum=1)
        if not picked:
            return
        domain, members = picked
        member_set = set(members)
        outsiders = [a for a in accounts if a not in member_set]
        if not outsiders:
            return
        account = accounts[choice(outsiders)]
        domain_id = domain.domain_id
    else:  # fake_domain — a domain that does not exist
        account = choice(list(accounts.values()))
        domain_id = params.fake_id()

    flags = int(OfferCreateFlag.TF_HYBRID) if hybrid else 0
    txn = OfferCreate(
        account=account.address,
        taker_gets=params.offer_xrp_drops(),
        taker_pays=taker_pays,
        domain_id=domain_id,
        flags=flags,
    )
    await submit_tx(name, txn, client, account.wallet)


# ── Direct domain payments (XRP) ────────────────────────────────────


async def payment_domain(
    accounts: dict[str, UserAccount],
    domains: list[PermissionedDomain],
    credentials: list[Credential],
    client: AsyncJsonRpcClient,
) -> None:
    if params.should_send_faulty():
        return await _payment_domain_faulty(accounts, domains, credentials, client)
    return await _payment_domain_valid(accounts, domains, credentials, client)


async def _payment_domain_valid(
    accounts: dict[str, UserAccount],
    domains: list[PermissionedDomain],
    credentials: list[Credential],
    client: AsyncJsonRpcClient,
) -> None:
    # A DomainID payment requires BOTH parties in-domain, so we need >=2 members.
    picked = _pick_domain_with_members(domains, accounts, credentials, minimum=2)
    if not picked:
        return
    domain, members = picked
    src_id, dst_id = sample(members, 2)
    src = accounts[src_id]
    txn = Payment(
        account=src.address,
        destination=dst_id,
        amount=params.payment_amount(),
        domain_id=domain.domain_id,
    )
    await submit_tx("PaymentDomain", txn, client, src.wallet)


async def _payment_domain_faulty(
    accounts: dict[str, UserAccount],
    domains: list[PermissionedDomain],
    credentials: list[Credential],
    client: AsyncJsonRpcClient,
) -> None:
    if len(accounts) < 2:
        return

    mutation = choice(["outsider_party", "fake_domain"])
    if mutation == "outsider_party":
        # One in-domain member + one outsider -> tecNO_PERMISSION.
        picked = _pick_domain_with_members(domains, accounts, credentials, minimum=1)
        if not picked:
            return
        domain, members = picked
        member_set = set(members)
        outsiders = [a for a in accounts if a not in member_set]
        if not outsiders:
            return
        src = accounts[choice(members)]
        dst_id = choice(outsiders)
        domain_id = domain.domain_id
    else:  # fake_domain — neither party can be in a domain that does not exist
        src_id, dst_id = sample(list(accounts), 2)
        src = accounts[src_id]
        domain_id = params.fake_id()

    txn = Payment(
        account=src.address,
        destination=dst_id,
        amount=params.payment_amount(),
        domain_id=domain_id,
    )
    await submit_tx("PaymentDomain", txn, client, src.wallet)


# ── Cross-currency domain payments (best-effort, Phase 4) ───────────


async def payment_domain_xc(
    accounts: dict[str, UserAccount],
    domains: list[PermissionedDomain],
    credentials: list[Credential],
    amms: list[AMM],
    client: AsyncJsonRpcClient,
) -> None:
    """Cross-currency Payment + DomainID — exercises domain pathfinding (routing
    through the domain order book). Success requires resting domain liquidity in
    the matching direction, which is not guaranteed here, so PaymentDomainXC is
    listed in assertions._NO_SUCCESS_TYPES; the common result is a tec failure
    (tecPATH_DRY / tecNO_PERMISSION), which still satisfies the failure bucket
    and exercises the both-parties-in-domain preclaim + pathfinding code."""
    if not amms:
        return
    picked = _pick_domain_with_members(domains, accounts, credentials, minimum=2)
    if not picked:
        return
    iou = _amm_iou(amms)
    if iou is None:
        return
    domain, members = picked
    src_id, dst_id = sample(members, 2)
    src = accounts[src_id]
    # Sender pays XRP (always fundable) via SendMax; deliver a small IOU amount
    # to the destination, routed through the domain book.
    deliver = IOUAmount(currency=iou.currency, issuer=iou.issuer, value=params.offer_iou_value())
    txn = Payment(
        account=src.address,
        destination=dst_id,
        amount=deliver,
        send_max=params.offer_xrp_drops(),
        domain_id=domain.domain_id,
    )
    await submit_tx("PaymentDomainXC", txn, client, src.wallet)

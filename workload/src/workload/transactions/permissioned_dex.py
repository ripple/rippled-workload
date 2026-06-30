"""Permissioned DEX handlers (featurePermissionedDEX): domain/hybrid offers and domain payments."""

from __future__ import annotations

from collections.abc import Callable

from xrpl.asyncio.clients import AsyncJsonRpcClient
from xrpl.models import IssuedCurrencyAmount as IOUAmount
from xrpl.models.currencies import IssuedCurrency
from xrpl.models.transactions import OfferCreate, Payment
from xrpl.models.transactions.offer_create import OfferCreateFlag
from xrpl.wallet import Wallet

from workload import params
from workload.fuzz import submit_fuzzed
from workload.models import AMM, Credential, PermissionedDomain, UserAccount
from workload.randoms import choice, random, sample
from workload.submit import submit_raw, submit_tx

# ── Membership helpers ──────────────────────────────────────────────


def _domain_members(
    domain: PermissionedDomain,
    accounts: dict[str, UserAccount],
    credentials: list[Credential],
) -> list[str]:
    """Owner plus holders of an accepted credential matching the domain's pairs; ours only."""
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
    """A controlled-owner domain with ≥ minimum members, plus its members; else None."""
    candidates = []
    for d in domains:
        if d.owner not in accounts:
            continue
        members = _domain_members(d, accounts, credentials)
        if len(members) >= minimum:
            candidates.append((d, members))
    return choice(candidates) if candidates else None


def _amm_iou(amms: list[AMM]) -> IssuedCurrency | None:
    if not amms:
        return None
    amm = choice(amms)
    ious = [a for a in amm.assets if isinstance(a, IssuedCurrency)]
    return choice(ious) if ious else None


def _domain_offer_flags(hybrid: bool) -> int:
    """tfPassive/tfSell still rest, so they keep the success path reliable."""
    flags = int(OfferCreateFlag.TF_HYBRID) if hybrid else 0
    if random() < 0.3:
        flags |= int(OfferCreateFlag.TF_PASSIVE)
    if random() < 0.3:
        flags |= int(OfferCreateFlag.TF_SELL)
    return flags


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


def _domain_offer_base(
    accounts: dict[str, UserAccount],
    domains: list[PermissionedDomain],
    credentials: list[Credential],
    amms: list[AMM],
    *,
    hybrid: bool,
) -> tuple[OfferCreate, Wallet] | None:
    """Valid domain offer (member trades XRP for a gateway IOU) + wallet."""
    picked = _pick_domain_with_members(domains, accounts, credentials, minimum=1)
    if not picked:
        return None
    iou = _amm_iou(amms)
    if iou is None:
        return None
    domain, members = picked
    member = accounts[choice(members)]
    # Empty domain book, so the offer rests cleanly.
    base = OfferCreate(
        account=member.address,
        taker_gets=params.offer_xrp_drops(),
        taker_pays=IOUAmount(
            currency=iou.currency, issuer=iou.issuer, value=params.offer_iou_value()
        ),
        domain_id=domain.domain_id,
        flags=_domain_offer_flags(hybrid),
    )
    return base, member.wallet


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
    built = _domain_offer_base(accounts, domains, credentials, amms, hybrid=hybrid)
    if built is None:
        return
    base, wallet = built
    await submit_tx(name, base, client, wallet)


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
    flags = int(OfferCreateFlag.TF_HYBRID) if hybrid else 0
    mutate: Callable[[dict], None] | None = None

    mutations = ["not_in_domain", "fake_domain", "zero_domain", "ioc_killed", "fuzz"]
    if hybrid:
        mutations.append("hybrid_no_domain")
    mutation = choice(mutations)

    if mutation == "fuzz":
        built = _domain_offer_base(accounts, domains, credentials, amms, hybrid=hybrid)
        if built is None:
            return
        base, wallet = built
        await submit_fuzzed(name, base, client, wallet)
        return

    if mutation == "not_in_domain":
        # Non-member submits to a real domain -> tecNO_PERMISSION.
        picked = _pick_domain_with_members(domains, accounts, credentials, minimum=1)
        if not picked:
            return
        domain, members = picked
        outsiders = [a for a in accounts if a not in set(members)]
        if not outsiders:
            return
        account = accounts[choice(outsiders)]
        domain_id = domain.domain_id
    elif mutation == "fake_domain":
        # Nonexistent domain -> tecNO_PERMISSION.
        account = choice(list(accounts.values()))
        domain_id = params.fake_id()
    elif mutation == "zero_domain":
        # temMALFORMED (fixCleanup3_2_0) / zero-key path otherwise.
        account = choice(list(accounts.values()))
        domain_id = params.zero_domain_id()
    elif mutation == "ioc_killed":
        # IoC offer can't cross the empty domain book -> tecKILLED.
        picked = _pick_domain_with_members(domains, accounts, credentials, minimum=1)
        if not picked:
            return
        domain, members = picked
        account = accounts[choice(members)]
        domain_id = domain.domain_id
        flags |= int(OfferCreateFlag.TF_IMMEDIATE_OR_CANCEL)
    else:  # hybrid_no_domain — tfHybrid without DomainID -> temINVALID_FLAG.
        picked = _pick_domain_with_members(domains, accounts, credentials, minimum=1)
        domain_id = picked[0].domain_id if picked else params.fake_id()
        account = choice(list(accounts.values()))

        def _mutate(d: dict) -> None:
            d.pop("DomainID", None)

        mutate = _mutate

    base = OfferCreate(
        account=account.address,
        taker_gets=params.offer_xrp_drops(),
        taker_pays=taker_pays,
        domain_id=domain_id,
        flags=flags,
    )
    await submit_raw(name, base, client, account.wallet, mutate)


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


def _domain_payment_base(
    accounts: dict[str, UserAccount],
    domains: list[PermissionedDomain],
    credentials: list[Credential],
) -> tuple[Payment, Wallet] | None:
    """Direct XRP payment between two in-domain members + wallet; shared by valid and fuzz."""
    picked = _pick_domain_with_members(domains, accounts, credentials, minimum=2)
    if not picked:
        return None
    domain, members = picked
    src_id, dst_id = sample(members, 2)
    src = accounts[src_id]
    base = Payment(
        account=src.address,
        destination=dst_id,
        amount=params.payment_amount(),
        domain_id=domain.domain_id,
    )
    return base, src.wallet


async def _payment_domain_valid(
    accounts: dict[str, UserAccount],
    domains: list[PermissionedDomain],
    credentials: list[Credential],
    client: AsyncJsonRpcClient,
) -> None:
    built = _domain_payment_base(accounts, domains, credentials)
    if built is None:
        return
    base, wallet = built
    await submit_tx("PaymentDomain", base, client, wallet)


async def _payment_domain_faulty(
    accounts: dict[str, UserAccount],
    domains: list[PermissionedDomain],
    credentials: list[Credential],
    client: AsyncJsonRpcClient,
) -> None:
    if len(accounts) < 2:
        return

    mutation = choice(["outsider_party", "fake_domain", "zero_domain", "fuzz"])
    if mutation == "fuzz":
        built = _domain_payment_base(accounts, domains, credentials)
        if built is None:
            return
        base, wallet = built
        await submit_fuzzed("PaymentDomain", base, client, wallet)
        return
    if mutation == "outsider_party":
        # One member + one outsider -> tecNO_PERMISSION.
        picked = _pick_domain_with_members(domains, accounts, credentials, minimum=1)
        if not picked:
            return
        domain, members = picked
        outsiders = [a for a in accounts if a not in set(members)]
        if not outsiders:
            return
        src = accounts[choice(members)]
        dst_id = choice(outsiders)
        domain_id = domain.domain_id
    elif mutation == "fake_domain":
        # Nonexistent domain -> tecNO_PERMISSION.
        src_id, dst_id = sample(list(accounts), 2)
        src = accounts[src_id]
        domain_id = params.fake_id()
    else:  # zero_domain — temMALFORMED / zero-key path
        src_id, dst_id = sample(list(accounts), 2)
        src = accounts[src_id]
        domain_id = params.zero_domain_id()

    base = Payment(
        account=src.address,
        destination=dst_id,
        amount=params.payment_amount(),
        domain_id=domain_id,
    )
    await submit_raw("PaymentDomain", base, client, src.wallet)


# ── Cross-currency domain payments (best-effort, Phase 4) ───────────


async def payment_domain_xc(
    accounts: dict[str, UserAccount],
    domains: list[PermissionedDomain],
    credentials: list[Credential],
    amms: list[AMM],
    client: AsyncJsonRpcClient,
) -> None:
    """Cross-currency Payment + DomainID for domain pathfinding. No guaranteed liquidity, so
    success is unreliable (in _NO_SUCCESS_TYPES); usual result is a tec failure."""
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
    # Sender pays XRP via SendMax; deliver an IOU routed through the domain book.
    deliver = IOUAmount(currency=iou.currency, issuer=iou.issuer, value=params.offer_iou_value())
    txn = Payment(
        account=src.address,
        destination=dst_id,
        amount=deliver,
        send_max=params.offer_xrp_drops(),
        domain_id=domain.domain_id,
    )
    await submit_tx("PaymentDomainXC", txn, client, src.wallet)

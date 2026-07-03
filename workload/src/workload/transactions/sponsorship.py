"""Sponsored Fees & Reserves transaction generators (XLS-68).

Three real transaction types: ``SponsorshipSet`` (create/update/delete the
prefunded ``Sponsorship`` object), ``SponsorshipTransfer`` (move/end reserve
sponsorship of an existing object or account), and ``Payment`` with
``tfSponsorCreatedAccount`` (sender sponsors a brand-new account's reserve).
Three of the five endpoints below are synthetic-name buckets on top of these
(see CLAUDE.md "Synthetic assertion names"): ``SponsorshipSetDelete``
discriminates via ``tfDeleteObject`` on a ``SponsorshipSet``,
``SponsorshipTransferAccount`` via absence of ``ObjectID`` on a
``SponsorshipTransfer``, ``PaymentSponsoredAccount`` via
``tfSponsorCreatedAccount`` on a ``Payment`` -- ws_listener.py maps validated
results back to the right bucket.

A sixth endpoint, ``sponsorship_audit_random``, isn't a transaction at all --
it's a read-only ``ledger_entry``/``account_info`` cross-check of tracked
sponsor state against the validated ledger, wired directly in app.py instead
of REGISTRY (see that function's docstring).
"""

from __future__ import annotations

from antithesis.lifecycle import send_event
from xrpl.asyncio.clients import AsyncJsonRpcClient
from xrpl.asyncio.transaction import autofill_and_sign
from xrpl.asyncio.transaction import submit as xrpl_submit
from xrpl.models import IssuedCurrencyAmount
from xrpl.models.requests import AccountInfo, LedgerEntry, ServerInfo
from xrpl.models.requests.ledger_entry import Sponsorship as LedgerEntrySponsorship
from xrpl.models.transactions import (
    Payment,
    PaymentFlag,
    SponsorshipSet,
    SponsorshipSetFlag,
    SponsorshipTransfer,
    SponsorshipTransferFlag,
)
from xrpl.transaction import sign_as_sponsor
from xrpl.wallet import Wallet

from workload import params
from workload.assertions import assert_sponsorship_audit, tx_submitted
from workload.fuzz import submit_fuzzed
from workload.models import (
    Check,
    Credential,
    Escrow,
    PaymentChannel,
    Sponsorship,
    TrustLine,
    UserAccount,
)
from workload.randoms import choice, random, sample
from workload.submit import submit_raw, submit_tx

# Populated by _payment_sponsored_account_valid, consumed by the "Payment" real-type
# state updater in transactions/__init__.py once the new account validates -- the
# same module-level-stash pattern as confidential_mpt.py's _pending_send_amounts.
_pending_sponsored_account_wallets: dict[str, Wallet] = {}

# ── shared helpers ───────────────────────────────────────────────────


def _object_candidates(
    checks: list[Check],
    escrows: list[Escrow],
    payment_channels: list[PaymentChannel],
    trust_lines: list[TrustLine],
    credentials: list[Credential],
) -> list[tuple[str, str]]:
    """(object_id, owner) pairs for sponsorable ledger objects this workload
    tracks. Trust lines are bidirectional; account_a stands in for "the
    owner" here (High/Low-specific dual tracking is a later stage). A
    credential's reserve owner follows acceptance: the issuer pays until the
    subject accepts, then the subject does (matches getLedgerEntryOwner)."""
    out: list[tuple[str, str]] = []
    out += [(c.check_id, c.creator) for c in checks]
    out += [(e.escrow_id, e.owner) for e in escrows if e.escrow_id]
    out += [(pc.channel_id, pc.source) for pc in payment_channels]
    out += [(tl.trust_line_id, tl.account_a) for tl in trust_lines if tl.trust_line_id]
    out += [
        (c.credential_id, c.subject if c.accepted else c.issuer)
        for c in credentials
        if c.credential_id
    ]
    return out


def _unsponsored_and_sponsored(
    checks: list[Check],
    escrows: list[Escrow],
    payment_channels: list[PaymentChannel],
    trust_lines: list[TrustLine],
    credentials: list[Credential],
    sponsored_objects: dict[str, tuple[str, str]],
) -> tuple[list[tuple[str, str]], list[tuple[str, tuple[str, str]]]]:
    candidates = _object_candidates(checks, escrows, payment_channels, trust_lines, credentials)
    unsponsored = [(oid, owner) for oid, owner in candidates if oid not in sponsored_objects]
    sponsored = list(sponsored_objects.items())
    return unsponsored, sponsored


def _pick_reserve_sponsor(
    owner_addr: str, accounts: dict[str, UserAccount], sponsorships: list[Sponsorship]
) -> tuple[str | None, bool]:
    """Pick a reserve sponsor for ``owner_addr``: prefer an existing prefunded
    Sponsorship with budget left (~50%), else fall back to a random co-signing
    sponsor. Returns ``(address, is_prefunded)``; address is None if no
    candidate account exists at all."""
    prefunded = [
        s
        for s in sponsorships
        if s.sponsee == owner_addr
        and s.sponsor != owner_addr
        and s.sponsor in accounts
        and s.remaining_owner_count > 0
        and not s.require_sign_for_reserve
    ]
    if prefunded and random() < 0.5:
        return choice(prefunded).sponsor, True
    others = [a for a in accounts if a != owner_addr]
    if not others:
        return None, False
    return choice(others), False


async def _submit_transfer_prefunded(
    name: str,
    sponsee: UserAccount,
    sponsor_addr: str,
    object_id: str | None,
    flag: SponsorshipTransferFlag,
    client: AsyncJsonRpcClient,
) -> None:
    """A prefunded Sponsorship with budget lets the sponsee submit alone (XLS-68
    spec 3.3) -- reserve-only flags keep this path independent of the paired
    Sponsorship's (possibly empty) fee pool."""
    txn = SponsorshipTransfer(
        account=sponsee.address,
        object_id=object_id,
        sponsor=sponsor_addr,
        sponsor_flags=params.SPF_SPONSOR_RESERVE,
        flags=flag,
    )
    await submit_tx(name, txn, client, sponsee.wallet)


async def _submit_transfer_cosigned(
    name: str,
    sponsee: UserAccount,
    sponsor: UserAccount,
    object_id: str | None,
    flag: SponsorshipTransferFlag,
    client: AsyncJsonRpcClient,
) -> None:
    """Dual-sign per sponsor_signer.py's documented flow: the sponsee autofills
    and signs first (SigningPubKey must already be set before the sponsor
    signs over the same canonical payload), then the sponsor adds
    SponsorSignature. submit_tx-bypass exception, same as LoanSet co-signing
    in lending.py."""
    txn = SponsorshipTransfer(
        account=sponsee.address,
        object_id=object_id,
        sponsor=sponsor.address,
        sponsor_flags=params.sponsor_reserve_flags(),
        flags=flag,
    )
    signed = await autofill_and_sign(txn, client, sponsee.wallet)
    sponsor_result = sign_as_sponsor(sponsor.wallet, signed)
    response = await xrpl_submit(sponsor_result.tx, client)
    tx_submitted(name, txn, response.result)


async def _submit_transfer_end(
    name: str,
    submitter: UserAccount,
    sponsee_addr: str | None,
    object_id: str | None,
    client: AsyncJsonRpcClient,
) -> None:
    """``sponsee_addr`` is only needed when the sponsor -- not the sponsee --
    is submitting End; it tells rippled which relationship to close. Passing
    ``sponsee=None`` is equivalent to omitting the field (xrpl-py drops
    None-valued optional fields at serialization)."""
    txn = SponsorshipTransfer(
        account=submitter.address,
        object_id=object_id,
        sponsee=sponsee_addr,
        flags=SponsorshipTransferFlag.TF_SPONSORSHIP_END,
    )
    await submit_tx(name, txn, client, submitter.wallet)


async def _drain_below_reserve(
    owner: UserAccount,
    other_addr: str,
    client: AsyncJsonRpcClient,
    *,
    reserve_delta_is_base: bool,
) -> bool:
    """Drain ``owner`` to just under the reserve threshold that a follow-up
    SponsorshipTransfer End would newly require, so it hits
    tecINSUFFICIENT_RESERVE. ``reserve_delta_is_base`` selects the
    account-level delta (dissolving adds a full base reserve, spec 6.2) vs.
    the object-level delta (one owner-reserve increment). Best-effort: any
    RPC hiccup aborts quietly -- this is exploratory state prep, not a
    correctness assertion."""
    try:
        info = await client.request(AccountInfo(account=owner.address))
        account_data = info.result.get("account_data", {})
        balance = int(account_data.get("Balance", "0"))
        owner_count = int(account_data.get("OwnerCount", 0))
        sponsored_count = int(account_data.get("SponsoredOwnerCount", 0))
        server = await client.request(ServerInfo())
        ledger_info = server.result.get("info", {}).get("validated_ledger", {})
        reserve_base = int(float(ledger_info.get("reserve_base_xrp", 1)) * 1_000_000)
        reserve_inc = int(float(ledger_info.get("reserve_inc_xrp", 0.2)) * 1_000_000)
    except Exception:
        return False
    # OwnerCount already counts sponsored objects; SponsoredOwnerCount nets them
    # back out since the sponsor -- not the owner -- currently pays for them.
    current_required = reserve_inc * max(0, owner_count - sponsored_count)
    delta = reserve_base if reserve_delta_is_base else reserve_inc
    target_balance = current_required + max(1, delta // 2)
    drain_amount = balance - target_balance - 20  # headroom for this payment's own fee
    if drain_amount <= 0:
        return False
    txn = Payment(account=owner.address, destination=other_addr, amount=str(drain_amount))
    await submit_tx("Payment", txn, client, owner.wallet)
    return True


# ── SponsorshipSet: create / update ──────────────────────────────────


async def sponsorship_set(
    accounts: dict[str, UserAccount],
    sponsorships: list[Sponsorship],
    client: AsyncJsonRpcClient,
) -> None:
    if params.should_send_faulty():
        return await _sponsorship_set_faulty(accounts, sponsorships, client)
    return await _sponsorship_set_valid(accounts, sponsorships, client)


def _pick_sponsor_sponsee(
    accounts: dict[str, UserAccount], sponsorships: list[Sponsorship]
) -> tuple[str, str] | None:
    # ~50% refill an existing tracked pair (doubles as pool refill); else a
    # fresh random pair, which rippled treats as create.
    if sponsorships and random() < 0.5:
        s = choice(sponsorships)
        if s.sponsor in accounts and s.sponsee in accounts:
            return s.sponsor, s.sponsee
    if len(accounts) < 2:
        return None
    sponsor_addr, sponsee_addr = sample(list(accounts), 2)
    return sponsor_addr, sponsee_addr


def _sponsorship_set_base(
    accounts: dict[str, UserAccount], sponsorships: list[Sponsorship]
) -> tuple[SponsorshipSet, Wallet] | None:
    pair = _pick_sponsor_sponsee(accounts, sponsorships)
    if pair is None:
        return None
    sponsor_addr, sponsee_addr = pair
    sponsor = accounts[sponsor_addr]
    txn = SponsorshipSet(
        account=sponsor.address,
        sponsee=sponsee_addr,
        fee_amount=params.sponsorship_fee_amount(),
        max_fee=params.sponsorship_max_fee(),
        remaining_owner_count=params.sponsorship_reserve_count(),
        flags=params.sponsorship_set_flags(),
    )
    return txn, sponsor.wallet


async def _sponsorship_set_valid(
    accounts: dict[str, UserAccount], sponsorships: list[Sponsorship], client: AsyncJsonRpcClient
) -> None:
    built = _sponsorship_set_base(accounts, sponsorships)
    if built is None:
        return
    txn, wallet = built
    await submit_tx("SponsorshipSet", txn, client, wallet)


async def _sponsorship_set_faulty(
    accounts: dict[str, UserAccount], sponsorships: list[Sponsorship], client: AsyncJsonRpcClient
) -> None:
    if len(accounts) < 2:
        return
    mutation = choice(
        [
            "fuzz",
            "self_sponsorship",
            "both_fields",
            "neither_field",
            "sponsee_attempts_create",
            "conflicting_flags",
            "delete_with_fields",
            "delete_nonexistent",
            "nonexistent_sponsee",
        ]
    )

    if mutation == "fuzz":
        built = _sponsorship_set_base(accounts, sponsorships)
        if built is None:
            return
        base, wallet = built
        await submit_fuzzed("SponsorshipSet", base, client, wallet)
        return

    if mutation in (
        "self_sponsorship",
        "both_fields",
        "neither_field",
        "sponsee_attempts_create",
        "conflicting_flags",
        "delete_with_fields",
    ):
        # xrpl-py's SponsorshipSet._get_errors rejects every one of these at
        # construction time, so only rippled's raw preflight can be exercised.
        built = _sponsorship_set_base(accounts, sponsorships)
        if built is None:
            return
        base, wallet = built

        def _mutate(d: dict) -> None:
            if mutation == "self_sponsorship":
                d["Sponsee"] = d["Account"]
            elif mutation == "both_fields":
                d["CounterpartySponsor"] = params.fake_account()
            elif mutation == "neither_field":
                d.pop("Sponsee", None)
            elif mutation == "sponsee_attempts_create":
                d["CounterpartySponsor"] = d.pop("Sponsee")
            elif mutation == "conflicting_flags":
                d["Flags"] = (
                    params.TF_SPONSORSHIP_SET_REQUIRE_SIGN_FOR_FEE
                    | params.TF_SPONSORSHIP_CLEAR_REQUIRE_SIGN_FOR_FEE
                )
            elif mutation == "delete_with_fields":
                d["Flags"] = d.get("Flags", 0) | params.TF_SPONSORSHIP_DELETE_OBJECT

        await submit_raw("SponsorshipSet", base, client, wallet, _mutate)
        return

    if mutation == "delete_nonexistent":
        # Two fresh random accounts almost never have a tracked relationship.
        sponsor_addr, sponsee_addr = sample(list(accounts), 2)
        txn = SponsorshipSet(
            account=sponsor_addr,
            sponsee=sponsee_addr,
            flags=SponsorshipSetFlag.TF_DELETE_OBJECT,
        )
        await submit_tx("SponsorshipSet", txn, client, accounts[sponsor_addr].wallet)
        return

    # nonexistent_sponsee: syntactically valid AccountID, absent from the
    # ledger -> tecNO_DST (rippled's actual code; the draft spec says
    # terNO_ACCOUNT, see Sponsor_test.cpp).
    sponsor = choice(list(accounts.values()))
    txn = SponsorshipSet(
        account=sponsor.address,
        sponsee=params.fake_account(),
        fee_amount=params.sponsorship_fee_amount(),
    )
    await submit_tx("SponsorshipSet", txn, client, sponsor.wallet)


# ── SponsorshipSet: delete (synthetic bucket; real type is SponsorshipSet) ──


async def sponsorship_set_delete(
    accounts: dict[str, UserAccount],
    sponsorships: list[Sponsorship],
    client: AsyncJsonRpcClient,
) -> None:
    if params.should_send_faulty():
        return await _sponsorship_set_delete_faulty(accounts, sponsorships, client)
    return await _sponsorship_set_delete_valid(accounts, sponsorships, client)


def _sponsorship_set_delete_base(
    accounts: dict[str, UserAccount], sponsorships: list[Sponsorship]
) -> tuple[SponsorshipSet, Wallet] | None:
    if not sponsorships:
        return None
    s = choice(sponsorships)
    candidates = [a for a in (s.sponsor, s.sponsee) if a in accounts]
    if not candidates:
        return None
    submitter_addr = choice(candidates)
    if submitter_addr == s.sponsor:
        txn = SponsorshipSet(
            account=s.sponsor, sponsee=s.sponsee, flags=SponsorshipSetFlag.TF_DELETE_OBJECT
        )
    else:
        txn = SponsorshipSet(
            account=s.sponsee,
            counterparty_sponsor=s.sponsor,
            flags=SponsorshipSetFlag.TF_DELETE_OBJECT,
        )
    return txn, accounts[submitter_addr].wallet


async def _sponsorship_set_delete_valid(
    accounts: dict[str, UserAccount], sponsorships: list[Sponsorship], client: AsyncJsonRpcClient
) -> None:
    built = _sponsorship_set_delete_base(accounts, sponsorships)
    if built is None:
        return
    txn, wallet = built
    await submit_tx("SponsorshipSetDelete", txn, client, wallet)


async def _sponsorship_set_delete_faulty(
    accounts: dict[str, UserAccount], sponsorships: list[Sponsorship], client: AsyncJsonRpcClient
) -> None:
    if not accounts:
        return
    mutation = choice(
        ["fuzz", "delete_nonexistent", "delete_with_extra_flag", "delete_with_fields"]
    )

    if mutation == "fuzz":
        built = _sponsorship_set_delete_base(accounts, sponsorships)
        if built is None:
            return
        base, wallet = built
        await submit_fuzzed("SponsorshipSetDelete", base, client, wallet)
        return

    if mutation == "delete_nonexistent":
        if len(accounts) < 2:
            return
        sponsor_addr, sponsee_addr = sample(list(accounts), 2)
        txn = SponsorshipSet(
            account=sponsor_addr,
            sponsee=sponsee_addr,
            flags=SponsorshipSetFlag.TF_DELETE_OBJECT,
        )
        await submit_tx("SponsorshipSetDelete", txn, client, accounts[sponsor_addr].wallet)
        return

    built = _sponsorship_set_delete_base(accounts, sponsorships)
    if built is None:
        return
    base, wallet = built

    def _mutate(d: dict) -> None:
        if mutation == "delete_with_extra_flag":
            d["Flags"] = d.get("Flags", 0) | params.TF_SPONSORSHIP_SET_REQUIRE_SIGN_FOR_FEE
        else:  # delete_with_fields
            d["FeeAmount"] = params.sponsorship_fee_amount()

    await submit_raw("SponsorshipSetDelete", base, client, wallet, _mutate)


# ── SponsorshipTransfer: object-level ────────────────────────────────


async def sponsorship_transfer(
    accounts: dict[str, UserAccount],
    sponsorships: list[Sponsorship],
    checks: list[Check],
    escrows: list[Escrow],
    payment_channels: list[PaymentChannel],
    trust_lines: list[TrustLine],
    credentials: list[Credential],
    sponsored_objects: dict[str, tuple[str, str]],
    offers: list[dict],
    client: AsyncJsonRpcClient,
) -> None:
    if params.should_send_faulty():
        return await _sponsorship_transfer_faulty(
            accounts,
            sponsorships,
            checks,
            escrows,
            payment_channels,
            trust_lines,
            credentials,
            sponsored_objects,
            offers,
            client,
        )
    return await _sponsorship_transfer_valid(
        accounts,
        sponsorships,
        checks,
        escrows,
        payment_channels,
        trust_lines,
        credentials,
        sponsored_objects,
        client,
    )


def _transfer_end_base(
    accounts: dict[str, UserAccount], sponsored_objects: dict[str, tuple[str, str]]
) -> tuple[SponsorshipTransfer, Wallet] | None:
    if not sponsored_objects:
        return None
    object_id, (owner_addr, sponsor_addr) = choice(list(sponsored_objects.items()))
    submitter_options = [a for a in (owner_addr, sponsor_addr) if a in accounts]
    if not submitter_options:
        return None
    submitter_addr = choice(submitter_options)
    sponsee_addr = None if submitter_addr == owner_addr else owner_addr
    txn = SponsorshipTransfer(
        account=submitter_addr,
        object_id=object_id,
        sponsee=sponsee_addr,
        flags=SponsorshipTransferFlag.TF_SPONSORSHIP_END,
    )
    return txn, accounts[submitter_addr].wallet


async def _sponsorship_transfer_valid(
    accounts: dict[str, UserAccount],
    sponsorships: list[Sponsorship],
    checks: list[Check],
    escrows: list[Escrow],
    payment_channels: list[PaymentChannel],
    trust_lines: list[TrustLine],
    credentials: list[Credential],
    sponsored_objects: dict[str, tuple[str, str]],
    client: AsyncJsonRpcClient,
) -> None:
    unsponsored, sponsored = _unsponsored_and_sponsored(
        checks, escrows, payment_channels, trust_lines, credentials, sponsored_objects
    )
    unsponsored = [(oid, owner) for oid, owner in unsponsored if owner in accounts]

    # Weighted toward create early: reserve-transfer needs unsponsored objects
    # to exist, and the tracked pool only grows once some have gone through.
    flavors = []
    if unsponsored:
        flavors += ["create", "create"]
    if sponsored:
        flavors += ["end", "reassign"]
    if not flavors:
        return
    flavor = choice(flavors)

    if flavor == "create":
        object_id, owner_addr = choice(unsponsored)
        new_sponsor_addr, prefunded = _pick_reserve_sponsor(owner_addr, accounts, sponsorships)
        if new_sponsor_addr is None:
            return
        owner = accounts[owner_addr]
        flag = SponsorshipTransferFlag.TF_SPONSORSHIP_CREATE
        if prefunded:
            await _submit_transfer_prefunded(
                "SponsorshipTransfer", owner, new_sponsor_addr, object_id, flag, client
            )
        else:
            await _submit_transfer_cosigned(
                "SponsorshipTransfer", owner, accounts[new_sponsor_addr], object_id, flag, client
            )
        return

    object_id, (owner_addr, sponsor_addr) = choice(sponsored)

    if flavor == "end":
        submitter_options = [a for a in (owner_addr, sponsor_addr) if a in accounts]
        if not submitter_options:
            return
        submitter_addr = choice(submitter_options)
        sponsee_field = None if submitter_addr == owner_addr else owner_addr
        await _submit_transfer_end(
            "SponsorshipTransfer", accounts[submitter_addr], sponsee_field, object_id, client
        )
        return

    # reassign
    if owner_addr not in accounts:
        return
    new_sponsor_addr, prefunded = _pick_reserve_sponsor(owner_addr, accounts, sponsorships)
    if new_sponsor_addr is None:
        return
    owner = accounts[owner_addr]
    flag = SponsorshipTransferFlag.TF_SPONSORSHIP_REASSIGN
    if prefunded:
        await _submit_transfer_prefunded(
            "SponsorshipTransfer", owner, new_sponsor_addr, object_id, flag, client
        )
    else:
        await _submit_transfer_cosigned(
            "SponsorshipTransfer", owner, accounts[new_sponsor_addr], object_id, flag, client
        )


async def _sponsorship_transfer_faulty(
    accounts: dict[str, UserAccount],
    sponsorships: list[Sponsorship],
    checks: list[Check],
    escrows: list[Escrow],
    payment_channels: list[PaymentChannel],
    trust_lines: list[TrustLine],
    credentials: list[Credential],
    sponsored_objects: dict[str, tuple[str, str]],
    offers: list[dict],
    client: AsyncJsonRpcClient,
) -> None:
    if not accounts:
        return
    unsponsored, sponsored = _unsponsored_and_sponsored(
        checks, escrows, payment_channels, trust_lines, credentials, sponsored_objects
    )
    unsponsored = [(oid, owner) for oid, owner in unsponsored if owner in accounts]

    mutation = choice(
        [
            "fuzz",
            "zero_flags",
            "multiple_flags",
            "fake_object_id",
            "end_on_unsponsored",
            "create_on_sponsored",
            "reassign_on_unsponsored",
            "third_party_end",
            "unsupported_object_type",
            "end_insufficient_reserve",
        ]
    )

    if mutation == "fuzz":
        built = _transfer_end_base(accounts, sponsored_objects)
        if built is None:
            return
        base, wallet = built
        await submit_fuzzed("SponsorshipTransfer", base, client, wallet)
        return

    if mutation == "zero_flags":
        # xrpl-py only rejects >1 flag, not 0 -> reaches rippled's temINVALID_FLAG.
        acc = choice(list(accounts.values()))
        txn = SponsorshipTransfer(account=acc.address, flags=0)
        await submit_tx("SponsorshipTransfer", txn, client, acc.wallet)
        return

    if mutation == "multiple_flags":
        acc = choice(list(accounts.values()))
        base = SponsorshipTransfer(
            account=acc.address, flags=SponsorshipTransferFlag.TF_SPONSORSHIP_END
        )

        def _mutate(d: dict) -> None:
            d["Flags"] = params.TF_SPONSORSHIP_CREATE | params.TF_SPONSORSHIP_REASSIGN

        await submit_raw("SponsorshipTransfer", base, client, acc.wallet, _mutate)
        return

    if mutation == "fake_object_id":
        acc = choice(list(accounts.values()))
        txn = SponsorshipTransfer(
            account=acc.address,
            object_id=params.fake_id(),
            flags=SponsorshipTransferFlag.TF_SPONSORSHIP_END,
        )
        await submit_tx("SponsorshipTransfer", txn, client, acc.wallet)
        return

    if mutation == "end_on_unsponsored":
        if not unsponsored:
            return
        object_id, owner_addr = choice(unsponsored)
        owner = accounts[owner_addr]
        txn = SponsorshipTransfer(
            account=owner.address,
            object_id=object_id,
            flags=SponsorshipTransferFlag.TF_SPONSORSHIP_END,
        )
        await submit_tx("SponsorshipTransfer", txn, client, owner.wallet)
        return

    if mutation == "create_on_sponsored":
        if not sponsored:
            return
        object_id, (owner_addr, _sponsor_addr) = choice(sponsored)
        if owner_addr not in accounts:
            return
        new_sponsor_addr, prefunded = _pick_reserve_sponsor(owner_addr, accounts, sponsorships)
        if new_sponsor_addr is None:
            return
        owner = accounts[owner_addr]
        flag = SponsorshipTransferFlag.TF_SPONSORSHIP_CREATE
        if prefunded:
            await _submit_transfer_prefunded(
                "SponsorshipTransfer", owner, new_sponsor_addr, object_id, flag, client
            )
        else:
            await _submit_transfer_cosigned(
                "SponsorshipTransfer", owner, accounts[new_sponsor_addr], object_id, flag, client
            )
        return

    if mutation == "reassign_on_unsponsored":
        if not unsponsored:
            return
        object_id, owner_addr = choice(unsponsored)
        new_sponsor_addr, prefunded = _pick_reserve_sponsor(owner_addr, accounts, sponsorships)
        if new_sponsor_addr is None:
            return
        owner = accounts[owner_addr]
        flag = SponsorshipTransferFlag.TF_SPONSORSHIP_REASSIGN
        if prefunded:
            await _submit_transfer_prefunded(
                "SponsorshipTransfer", owner, new_sponsor_addr, object_id, flag, client
            )
        else:
            await _submit_transfer_cosigned(
                "SponsorshipTransfer", owner, accounts[new_sponsor_addr], object_id, flag, client
            )
        return

    if mutation == "third_party_end":
        if not sponsored:
            return
        object_id, (owner_addr, sponsor_addr) = choice(sponsored)
        third_parties = [a for a in accounts if a not in (owner_addr, sponsor_addr)]
        if not third_parties:
            return
        third = accounts[choice(third_parties)]
        txn = SponsorshipTransfer(
            account=third.address,
            object_id=object_id,
            sponsee=owner_addr,
            flags=SponsorshipTransferFlag.TF_SPONSORSHIP_END,
        )
        await submit_tx("SponsorshipTransfer", txn, client, third.wallet)
        return

    if mutation == "unsupported_object_type":
        # Offer is a real ledger object we track but rippled's sponsorship
        # allow-list excludes it -> tecNO_PERMISSION.
        if not offers:
            return
        offer_id = choice(offers).get("offer_id")
        if not offer_id:
            return
        acc = choice(list(accounts.values()))
        txn = SponsorshipTransfer(
            account=acc.address,
            object_id=offer_id,
            flags=SponsorshipTransferFlag.TF_SPONSORSHIP_END,
        )
        await submit_tx("SponsorshipTransfer", txn, client, acc.wallet)
        return

    # end_insufficient_reserve: drain the owner first so re-absorbing the
    # object's reserve on End fails.
    if not sponsored:
        return
    object_id, (owner_addr, _sponsor_addr) = choice(sponsored)
    if owner_addr not in accounts:
        return
    owner = accounts[owner_addr]
    other_addr = next((a for a in accounts if a != owner_addr), None)
    if other_addr is None:
        return
    if not await _drain_below_reserve(owner, other_addr, client, reserve_delta_is_base=False):
        return
    txn = SponsorshipTransfer(
        account=owner.address,
        object_id=object_id,
        flags=SponsorshipTransferFlag.TF_SPONSORSHIP_END,
    )
    await submit_tx("SponsorshipTransfer", txn, client, owner.wallet)


# ── SponsorshipTransfer: account-level (synthetic bucket) ────────────


async def sponsorship_transfer_account(
    accounts: dict[str, UserAccount],
    sponsorships: list[Sponsorship],
    sponsored_accounts: dict[str, str],
    protected_accounts: set[str],
    client: AsyncJsonRpcClient,
) -> None:
    if params.should_send_faulty():
        return await _sponsorship_transfer_account_faulty(
            accounts, sponsorships, sponsored_accounts, protected_accounts, client
        )
    return await _sponsorship_transfer_account_valid(
        accounts, sponsorships, sponsored_accounts, protected_accounts, client
    )


def _account_pools(
    accounts: dict[str, UserAccount],
    sponsored_accounts: dict[str, str],
    protected_accounts: set[str],
) -> tuple[list[str], list[str]]:
    eligible = [a for a in accounts if a not in protected_accounts]
    unsponsored = [a for a in eligible if a not in sponsored_accounts]
    sponsored = [a for a in eligible if a in sponsored_accounts]
    return unsponsored, sponsored


def _transfer_end_account_base(
    accounts: dict[str, UserAccount], sponsored_accounts: dict[str, str]
) -> tuple[SponsorshipTransfer, Wallet] | None:
    if not sponsored_accounts:
        return None
    owner_addr, sponsor_addr = choice(list(sponsored_accounts.items()))
    submitter_options = [a for a in (owner_addr, sponsor_addr) if a in accounts]
    if not submitter_options:
        return None
    submitter_addr = choice(submitter_options)
    sponsee_addr = None if submitter_addr == owner_addr else owner_addr
    txn = SponsorshipTransfer(
        account=submitter_addr,
        sponsee=sponsee_addr,
        flags=SponsorshipTransferFlag.TF_SPONSORSHIP_END,
    )
    return txn, accounts[submitter_addr].wallet


async def _sponsorship_transfer_account_valid(
    accounts: dict[str, UserAccount],
    sponsorships: list[Sponsorship],
    sponsored_accounts: dict[str, str],
    protected_accounts: set[str],
    client: AsyncJsonRpcClient,
) -> None:
    unsponsored, sponsored = _account_pools(accounts, sponsored_accounts, protected_accounts)

    flavors = []
    if unsponsored and len(accounts) >= 2:
        flavors.append("create")
    if sponsored:
        flavors += ["end", "reassign"]
    if not flavors:
        return
    flavor = choice(flavors)

    if flavor == "create":
        owner_addr = choice(unsponsored)
        others = [a for a in accounts if a != owner_addr]
        if not others:
            return
        new_sponsor_addr = choice(others)
        # Account-level Create/Reassign always require co-sign -- rippled
        # rejects a prefunded budget here (temMALFORMED).
        await _submit_transfer_cosigned(
            "SponsorshipTransferAccount",
            accounts[owner_addr],
            accounts[new_sponsor_addr],
            None,
            SponsorshipTransferFlag.TF_SPONSORSHIP_CREATE,
            client,
        )
        return

    if flavor == "end":
        owner_addr = choice(sponsored)
        sponsor_addr = sponsored_accounts[owner_addr]
        submitter_options = [a for a in (owner_addr, sponsor_addr) if a in accounts]
        if not submitter_options:
            return
        submitter_addr = choice(submitter_options)
        sponsee_field = None if submitter_addr == owner_addr else owner_addr
        await _submit_transfer_end(
            "SponsorshipTransferAccount", accounts[submitter_addr], sponsee_field, None, client
        )
        return

    # reassign
    owner_addr = choice(sponsored)
    others = [a for a in accounts if a != owner_addr]
    if not others:
        return
    new_sponsor_addr = choice(others)
    await _submit_transfer_cosigned(
        "SponsorshipTransferAccount",
        accounts[owner_addr],
        accounts[new_sponsor_addr],
        None,
        SponsorshipTransferFlag.TF_SPONSORSHIP_REASSIGN,
        client,
    )


async def _sponsorship_transfer_account_faulty(
    accounts: dict[str, UserAccount],
    sponsorships: list[Sponsorship],
    sponsored_accounts: dict[str, str],
    protected_accounts: set[str],
    client: AsyncJsonRpcClient,
) -> None:
    if not accounts:
        return
    unsponsored, sponsored = _account_pools(accounts, sponsored_accounts, protected_accounts)

    mutation = choice(
        [
            "fuzz",
            "zero_flags",
            "create_without_cosign",
            "nonexistent_new_sponsor",
            "third_party_end",
            "end_insufficient_reserve",
        ]
    )

    if mutation == "fuzz":
        built = _transfer_end_account_base(accounts, sponsored_accounts)
        if built is None:
            return
        base, wallet = built
        await submit_fuzzed("SponsorshipTransferAccount", base, client, wallet)
        return

    if mutation == "zero_flags":
        acc = choice(list(accounts.values()))
        txn = SponsorshipTransfer(account=acc.address, flags=0)
        await submit_tx("SponsorshipTransferAccount", txn, client, acc.wallet)
        return

    if mutation == "create_without_cosign":
        # xrpl-py doesn't require sponsor_signature client-side (only object-
        # level prefunding is legal without it) -> reaches rippled's
        # account-level-specific temMALFORMED.
        if not unsponsored:
            return
        owner_addr = choice(unsponsored)
        others = [a for a in accounts if a != owner_addr]
        if not others:
            return
        new_sponsor_addr = choice(others)
        txn = SponsorshipTransfer(
            account=owner_addr,
            sponsor=new_sponsor_addr,
            sponsor_flags=params.sponsor_reserve_flags(),
            flags=SponsorshipTransferFlag.TF_SPONSORSHIP_CREATE,
        )
        await submit_tx("SponsorshipTransferAccount", txn, client, accounts[owner_addr].wallet)
        return

    if mutation == "nonexistent_new_sponsor":
        # A freshly-generated, never-funded wallet signs fine (no ledger
        # lookup needed to sign) but doesn't exist on-ledger -> terNO_ACCOUNT.
        if not unsponsored:
            return
        owner = accounts[choice(unsponsored)]
        fake_wallet = Wallet.create()
        txn = SponsorshipTransfer(
            account=owner.address,
            sponsor=fake_wallet.address,
            sponsor_flags=params.sponsor_reserve_flags(),
            flags=SponsorshipTransferFlag.TF_SPONSORSHIP_CREATE,
        )
        signed = await autofill_and_sign(txn, client, owner.wallet)
        sponsor_result = sign_as_sponsor(fake_wallet, signed)
        response = await xrpl_submit(sponsor_result.tx, client)
        tx_submitted("SponsorshipTransferAccount", txn, response.result)
        return

    if mutation == "third_party_end":
        if not sponsored:
            return
        owner_addr = choice(sponsored)
        sponsor_addr = sponsored_accounts[owner_addr]
        third_parties = [a for a in accounts if a not in (owner_addr, sponsor_addr)]
        if not third_parties:
            return
        third = accounts[choice(third_parties)]
        txn = SponsorshipTransfer(
            account=third.address,
            sponsee=owner_addr,
            flags=SponsorshipTransferFlag.TF_SPONSORSHIP_END,
        )
        await submit_tx("SponsorshipTransferAccount", txn, client, third.wallet)
        return

    # end_insufficient_reserve
    if not sponsored:
        return
    owner_addr = choice(sponsored)
    owner = accounts[owner_addr]
    other_addr = next((a for a in accounts if a != owner_addr), None)
    if other_addr is None:
        return
    if not await _drain_below_reserve(owner, other_addr, client, reserve_delta_is_base=True):
        return
    txn = SponsorshipTransfer(
        account=owner.address, flags=SponsorshipTransferFlag.TF_SPONSORSHIP_END
    )
    await submit_tx("SponsorshipTransferAccount", txn, client, owner.wallet)


# ── Payment + tfSponsorCreatedAccount (synthetic bucket; real type is Payment) ──


async def payment_sponsored_account(
    accounts: dict[str, UserAccount],
    client: AsyncJsonRpcClient,
) -> None:
    if params.should_send_faulty():
        return await _payment_sponsored_account_faulty(accounts, client)
    return await _payment_sponsored_account_valid(accounts, client)


def _payment_sponsored_account_base(
    accounts: dict[str, UserAccount],
) -> tuple[Payment, Wallet, Wallet] | None:
    """Valid sponsor-created-account Payment + sender wallet + fresh destination
    wallet; shared by valid and fuzz. Amount is below the base reserve -- the
    sponsor (the sender itself, via the flag) covers the reserve instead."""
    if not accounts:
        return None
    sender = choice(list(accounts.values()))
    dest_wallet = Wallet.create()
    txn = Payment(
        account=sender.address,
        destination=dest_wallet.address,
        amount=params.sponsored_account_amount(),
        flags=PaymentFlag.TF_SPONSOR_CREATED_ACCOUNT,
    )
    return txn, sender.wallet, dest_wallet


async def _payment_sponsored_account_valid(
    accounts: dict[str, UserAccount],
    client: AsyncJsonRpcClient,
) -> None:
    built = _payment_sponsored_account_base(accounts)
    if built is None:
        return
    txn, sender_wallet, dest_wallet = built
    result = await submit_tx("PaymentSponsoredAccount", txn, client, sender_wallet)

    # Stash only when the submit will likely apply, else the entry leaks (see
    # escrow.py's identical guard).
    engine = result.get("engine_result", "") if result else ""
    if engine in ("tesSUCCESS", "terQUEUED", "terPRE_SEQ"):
        _pending_sponsored_account_wallets[dest_wallet.address] = dest_wallet


async def _payment_sponsored_account_faulty(
    accounts: dict[str, UserAccount],
    client: AsyncJsonRpcClient,
) -> None:
    if not accounts:
        return
    mutation = choice(["fuzz", "existing_destination", "partial_payment_combo", "iou_amount"])

    if mutation == "fuzz":
        built = _payment_sponsored_account_base(accounts)
        if built is None:
            return
        base, wallet, _dest_wallet = built
        await submit_fuzzed("PaymentSponsoredAccount", base, client, wallet)
        return

    if mutation == "existing_destination":
        # The flag is only valid when creating a brand-new account -> tecNO_SPONSOR_PERMISSION.
        if len(accounts) < 2:
            return
        src_addr, dst_addr = sample(list(accounts), 2)
        txn = Payment(
            account=src_addr,
            destination=dst_addr,
            amount=params.sponsored_account_amount(),
            flags=PaymentFlag.TF_SPONSOR_CREATED_ACCOUNT,
        )
        await submit_tx("PaymentSponsoredAccount", txn, client, accounts[src_addr].wallet)
        return

    if mutation == "partial_payment_combo":
        # xrpl-py rejects this combo at construction -> raw submit for rippled's
        # preflight temINVALID_FLAG.
        built = _payment_sponsored_account_base(accounts)
        if built is None:
            return
        base, wallet, _dest_wallet = built

        def _mutate(d: dict) -> None:
            d["Flags"] = d.get("Flags", 0) | int(PaymentFlag.TF_PARTIAL_PAYMENT)

        await submit_raw("PaymentSponsoredAccount", base, client, wallet, _mutate)
        return

    # iou_amount: a non-native dstAmount with the flag -> temBAD_AMOUNT.
    sender = choice(list(accounts.values()))
    txn = Payment(
        account=sender.address,
        destination=Wallet.create().address,
        amount=IssuedCurrencyAmount(
            currency=params.currency_code(), issuer=sender.address, value=params.iou_amount()
        ),
        flags=PaymentFlag.TF_SPONSOR_CREATED_ACCOUNT,
    )
    await submit_tx("PaymentSponsoredAccount", txn, client, sender.wallet)


# ── Fee-sponsor helper for submit_tx ──────────────────────────────────

# Nominal per-use decrement for the local FeeAmount estimate; the real budget
# only refreshes when a SponsorshipSet on the same pair is observed (see
# _on_sponsorship_set), so this just keeps the estimate from drifting too high.
_SPONSORED_FEE_ESTIMATE_DROPS = 10


def maybe_sponsor(src_address: str, sponsorships: list[Sponsorship]) -> str | None:
    """Pick a prefunded fee sponsor for src_address's tx, or None. Prefunded-only:
    a co-signed sponsor needs its own signature, which this fire-and-forget hook
    can't obtain (that flow lives in the sponsorship.py endpoints instead)."""
    if not sponsorships or random() >= 0.10:
        return None
    candidates = [
        s
        for s in sponsorships
        if s.sponsee == src_address and s.fee_amount > 0 and not s.require_sign_for_fee
    ]
    if not candidates:
        return None
    s = choice(candidates)
    s.fee_amount = max(0, s.fee_amount - _SPONSORED_FEE_ESTIMATE_DROPS)
    return s.sponsor


# ── SponsorshipAudit: read-only ledger cross-check (not a transaction) ───────
# No engine_result, so it doesn't fit REGISTRY's seen/success/failure shape --
# app.py wires the route directly instead (see create_app). Both checks are
# soft by design: tracked state legitimately drifts (a Sponsorship/sponsored
# account can vanish through a path this workload's WS listener doesn't parse),
# so a genuine miss just prunes + records a `sometimes` sample rather than
# failing a flaky `always` (see assertions.assert_sponsorship_audit).


async def sponsorship_audit_random(
    sponsorships: list[Sponsorship],
    sponsored_accounts: dict[str, str],
    client: AsyncJsonRpcClient,
) -> None:
    await _audit_sponsorship_object(sponsorships, client)
    await _audit_sponsored_account(sponsored_accounts, client)


async def _audit_sponsorship_object(
    sponsorships: list[Sponsorship], client: AsyncJsonRpcClient
) -> None:
    if not sponsorships:
        return
    s = choice(sponsorships)
    resp = await client.request(
        LedgerEntry(
            sponsorship=LedgerEntrySponsorship(sponsor=s.sponsor, sponsee=s.sponsee),
            ledger_index="validated",
        )
    )
    if resp.result.get("error") == "entryNotFound":
        sponsorships[:] = [
            x for x in sponsorships if not (x.sponsor == s.sponsor and x.sponsee == s.sponsee)
        ]
        send_event(
            "workload::sponsorship_audit_pruned",
            {"kind": "object", "sponsor": s.sponsor, "sponsee": s.sponsee},
        )
        return
    assert_sponsorship_audit("object", resp.is_successful())


async def _audit_sponsored_account(
    sponsored_accounts: dict[str, str], client: AsyncJsonRpcClient
) -> None:
    if not sponsored_accounts:
        return
    owner_addr, sponsor_addr = choice(list(sponsored_accounts.items()))
    resp = await client.request(AccountInfo(account=owner_addr, ledger_index="validated"))
    if resp.result.get("error") == "actNotFound":
        sponsored_accounts.pop(owner_addr, None)
        send_event(
            "workload::sponsorship_audit_pruned",
            {"kind": "account", "account": owner_addr, "sponsor": sponsor_addr},
        )
        return
    has_sponsor = bool(resp.result.get("account_data", {}).get("Sponsor"))
    assert_sponsorship_audit("account", has_sponsor)

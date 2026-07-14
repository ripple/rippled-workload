"""Type-agnostic sponsor malformations (XLS-68).

One raw-submit endpoint carrying the sponsor faults xrpl-py rejects at
construction, which therefore cannot ride the submit-time sponsor Modifier (it
decorates a Transaction *model*). This replaces the raw-fault vectors the eight
bespoke ``Sponsored*Create`` handlers used to carry. Every vector is a preflight
``tem*`` (never enters a ledger), so the ``SponsorMalformation`` bucket only feeds
``seen`` + the submit-time ``sponsor_disallowed_type_rejected`` dimension (fired
generically in assertions.py); the type is in ``_NO_SUCCESS_TYPES`` /
``_NO_FAILURE_TYPES``.
"""

from __future__ import annotations

from xrpl.asyncio.clients import AsyncJsonRpcClient
from xrpl.models import IssuedCurrencyAmount as IOUAmount
from xrpl.models.transactions import DepositPreauth, OfferCreate

from workload import params
from workload.models import UserAccount
from workload.randoms import choice, sample
from workload.submit import submit_raw, submit_tx
from workload.transactions.nft import _nftoken_mint_base

_NAME = "SponsorMalformation"


async def sponsor_malformation(
    accounts: dict[str, UserAccount], client: AsyncJsonRpcClient
) -> None:
    if len(accounts) < 2:
        return
    vector = choice(
        ["invalid_flag_bits", "sponsor_equals_account", "flags_without_sponsor", "disallowed_type"]
    )
    if vector == "disallowed_type":
        return await _disallowed_type(accounts, client)

    # The other three share a valid reserve-sponsored DepositPreauth base (an
    # allow-listed type), mutated post-construction so rippled's preflight1Sponsor
    # does the rejecting.
    src_id, sponsor_id = sample(list(accounts), 2)
    src = accounts[src_id]
    base = DepositPreauth(
        account=src.address,
        authorize=params.fake_account(),
        sponsor=sponsor_id,
        sponsor_flags=params.sponsor_reserve_flags(),
    )

    def _mutate(d: dict) -> None:
        if vector == "invalid_flag_bits":
            # A bit outside spfSponsorFee/spfSponsorReserve -> temINVALID_FLAG.
            d["SponsorFlags"] = choice([0x4, 0x80000000, 0xFFFFFFFF])
        elif vector == "sponsor_equals_account":
            d["Sponsor"] = d["Account"]  # -> temMALFORMED
        else:  # flags_without_sponsor: hasSponsor != hasSponsorFlags -> temINVALID_FLAG
            d.pop("Sponsor", None)

    await submit_raw(_NAME, base, client, src.wallet, _mutate)


async def _disallowed_type(accounts: dict[str, UserAccount], client: AsyncJsonRpcClient) -> None:
    """A valid reserve sponsor on a type outside rippled's isReserveSponsorAllowed
    (OfferCreate / NFTokenMint) -> preflight1Sponsor temINVALID_FLAG. xrpl-py builds
    it fine, so submit_tx -- the SponsorMalformation name is excluded from every
    modifier, so the pipeline is a no-op and never adds a second sponsor."""
    if choice([True, False]):
        src_id, sponsor_id = sample(list(accounts), 2)
        src = accounts[src_id]
        txn = OfferCreate(
            account=src.address,
            taker_gets=params.offer_xrp_drops(),
            taker_pays=IOUAmount(
                currency=params.currency_code(),
                issuer=params.fake_account(),
                value=params.offer_iou_value(),
            ),
            sponsor=sponsor_id,
            sponsor_flags=params.sponsor_reserve_flags(),
        )
        await submit_tx(_NAME, txn, client, src.wallet)
        return
    built = _nftoken_mint_base(accounts)
    if built is None:
        return
    nft_txn, wallet = built
    others = [a for a in accounts if a != nft_txn.account]
    if not others:
        return
    txn = nft_txn.__replace__(sponsor=choice(others), sponsor_flags=params.sponsor_reserve_flags())
    await submit_tx(_NAME, txn, client, wallet)

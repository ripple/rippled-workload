"""AMM transaction generators for the antithesis workload.

MPT-on-DEX (XLS-82, Phase 4): MPT assets are folded into the existing AMM
handlers — no new synthetic names or REGISTRY entries. ``w.amms`` may now hold
MPT-paired pools (``_on_amm_create`` tracks them via ``_parse_asset``), so every
handler that does ``choice(amms)`` must build MPT amounts/assets correctly.
xrpl-py accepts ``MPTCurrency`` in ``asset``/``asset2`` and ``MPTAmount`` in
``amount``/``amount2``/``e_price`` for all six AMM models (verified at
construction AND binary-codec encode), so the policy is HANDLE everywhere — no
handler skips MPT pools.
"""

import xrpl.models
from xrpl.asyncio.clients import AsyncJsonRpcClient
from xrpl.models import IssuedCurrency
from xrpl.models import IssuedCurrencyAmount as IOUAmount
from xrpl.models.amounts import MPTAmount
from xrpl.models.currencies import MPTCurrency
from xrpl.models.transactions import (
    AMMBid,
    AMMCreate,
    AMMDelete,
    AMMDeposit,
    AMMVote,
    AMMWithdraw,
)
from xrpl.models.transactions.amm_bid import AuthAccount
from xrpl.models.transactions.amm_deposit import AMMDepositFlag
from xrpl.models.transactions.amm_withdraw import AMMWithdrawFlag

from workload import params
from workload.fuzz import submit_fuzzed
from workload.models import AMM, MPTokenIssuance, TrustLine, UserAccount
from workload.randoms import choice, randint, random, sample
from workload.submit import submit_raw, submit_tx
from workload.transactions.mpt_dex import (
    _controlled_holders,
    _locked,
    _no_trade,
    _require_auth,
    _tradeable,
)

# ── Helpers ──────────────────────────────────────────────────────────


def _find_account_with_trust_lines(
    accounts: dict[str, UserAccount],
    trust_lines: list[TrustLine],
    needed_ious: list[IssuedCurrency],
) -> UserAccount | None:
    """Find an account that has trust lines for all needed IOUs.

    Accounts with trust lines received IOU distributions during setup,
    so having the trust line implies having balance.
    Returns None if no eligible account exists.
    """
    if not needed_ious:
        return choice(list(accounts.values()))

    tl_by_account: dict[str, set[tuple[str, str]]] = {}
    for tl in trust_lines:
        tl_by_account.setdefault(tl.account_a, set()).add((tl.currency, tl.account_b))

    eligible = []
    for addr, acct in accounts.items():
        acct_tls = tl_by_account.get(addr, set())
        if all((iou.currency, iou.issuer) in acct_tls for iou in needed_ious):
            eligible.append(acct)

    if not eligible:
        return None
    return choice(eligible)


def _pick_asset_pair(
    trust_lines: list[TrustLine],
) -> tuple[IssuedCurrency | xrpl.models.XRP, IssuedCurrency] | None:
    """Pick a random asset pair for AMM creation.

    Returns (asset1, asset2) where asset1 may be XRP and asset2 is always IOU.
    Returns None if no trust lines exist.
    """
    if not trust_lines:
        return None
    tl = choice(trust_lines)
    iou = IssuedCurrency(currency=tl.currency, issuer=tl.account_b)
    if random() < 0.3:
        return xrpl.models.XRP(), iou
    # Pick a second different IOU
    other_tls = [t for t in trust_lines if t.currency != tl.currency or t.account_b != tl.account_b]
    if other_tls:
        tl2 = choice(other_tls)
        iou2 = IssuedCurrency(currency=tl2.currency, issuer=tl2.account_b)
        return iou, iou2
    return xrpl.models.XRP(), iou


def _fake_iou() -> IssuedCurrency:
    """Generate an IOU with a valid-format but non-existent issuer."""
    return IssuedCurrency(currency=params.currency_code(), issuer=params.fake_account())


def _amount_for(
    asset: IssuedCurrency | MPTCurrency | xrpl.models.XRP, value: str
) -> IOUAmount | MPTAmount | str:
    """Build an amount for the given asset.

    XRP -> drops string; IssuedCurrency -> IOUAmount; MPTCurrency -> MPTAmount.
    ``value`` is an integer-or-decimal string; MPT amounts must be integers, and
    all callers pass integer-valued ``params`` generators (amm_deposit_amount /
    amm_withdraw_amount / mpt-safe values), so an MPTAmount is always well-formed.
    """
    if isinstance(asset, xrpl.models.XRP):
        return value
    if isinstance(asset, MPTCurrency):
        return MPTAmount(mpt_issuance_id=asset.mpt_issuance_id, value=value)
    return IOUAmount(currency=asset.currency, issuer=asset.issuer, value=value)


def _iou_leg(amm: AMM) -> IssuedCurrency | None:
    """Return an IOU leg of ``amm`` (skip XRP and MPT), or None if none exists.

    Used by faulty vectors that build a signed-IOU amount (negative / overdraw)
    which xrpl-py only models for IssuedCurrencyAmount — those vectors must skip
    MPT-only pools rather than raise on ``MPTCurrency.currency``."""
    for a in amm.assets:
        if isinstance(a, IssuedCurrency):
            return a
    return None


# ── AMMCreate ────────────────────────────────────────────────────────


def _mpt_create_value() -> str:
    """Integer MPT value for an AMM create leg, modest within the 10000 setup
    distribution so a controlled holder can always fund it."""
    return params.amm_deposit_amount()


def _amm_create_mpt_base(
    accounts: dict[str, UserAccount],
    mpt_issuances: list[MPTokenIssuance],
) -> tuple[AMMCreate, UserAccount] | None:
    """Build a valid MPT-paired AMMCreate + its creator. Shared by the valid path
    and the fuzz faulty vector.

    Primary shape: MPT+XRP (always fundable — XRP leg from amm_xrp_amount, MPT
    leg from a tradeable issuance the creator is a controlled holder of). ~25%:
    MPT+MPT from two distinct tradeable issuances whose controlled-holder sets
    intersect (setup authorizes every holder for every issuance, so the holder
    set is the intersection). Returns None when no tradeable MPT/holder exists.
    """
    tr = _tradeable(mpt_issuances)
    if not tr or not accounts:
        return None
    mpt1 = choice(tr)
    holders1 = _controlled_holders(mpt1, accounts)
    if not holders1:
        return None
    mpt_amt1 = _amount_for(MPTCurrency(mpt_issuance_id=mpt1.mpt_issuance_id), _mpt_create_value())

    # ~25%: try MPT+MPT with a second distinct tradeable issuance.
    others = [m for m in tr if m.mpt_issuance_id != mpt1.mpt_issuance_id]
    if others and random() < 0.25:
        mpt2 = choice(others)
        shared = [h for h in holders1 if h in _controlled_holders(mpt2, accounts)]
        if shared:
            src = accounts[choice(shared)]
            mpt_amt2 = _amount_for(
                MPTCurrency(mpt_issuance_id=mpt2.mpt_issuance_id), _mpt_create_value()
            )
            base = AMMCreate(
                account=src.address,
                amount=mpt_amt1,
                amount2=mpt_amt2,
                trading_fee=params.amm_trading_fee(),
            )
            return base, src

    # MPT+XRP.
    src = accounts[choice(holders1)]
    base = AMMCreate(
        account=src.address,
        amount=mpt_amt1,
        amount2=params.amm_xrp_amount(),
        trading_fee=params.amm_trading_fee(),
    )
    return base, src


async def amm_create(
    accounts: dict[str, UserAccount],
    amms: list[AMM],
    trust_lines: list[TrustLine],
    mpt_issuances: list[MPTokenIssuance],
    client: AsyncJsonRpcClient,
) -> None:
    if params.should_send_faulty():
        return await _amm_create_faulty(accounts, amms, trust_lines, mpt_issuances, client)
    return await _amm_create_valid(accounts, amms, trust_lines, mpt_issuances, client)


async def _amm_create_valid(
    accounts: dict[str, UserAccount],
    amms: list[AMM],
    trust_lines: list[TrustLine],
    mpt_issuances: list[MPTokenIssuance],
    client: AsyncJsonRpcClient,
) -> None:
    if not accounts:
        return
    # ~35%: build an MPT-paired pool when a tradeable MPT exists. Repeated
    # same-pair creates hit tecDUPLICATE after the first — fine; the IOU/XRP
    # majority keeps the success bucket green.
    if _tradeable(mpt_issuances) and random() < 0.35:
        built = _amm_create_mpt_base(accounts, mpt_issuances)
        if built is not None:
            base, src = built
            await submit_tx("AMMCreate", base, client, src.wallet)
            return
    if not trust_lines:
        return
    pair = _pick_asset_pair(trust_lines)
    if not pair:
        return
    asset1, asset2 = pair
    # Find account that holds the needed IOUs
    needed = [a for a in [asset1, asset2] if not isinstance(a, xrpl.models.XRP)]
    src = _find_account_with_trust_lines(accounts, trust_lines, needed)
    if not src:
        return
    if isinstance(asset1, xrpl.models.XRP):
        amount1 = params.amm_xrp_amount()
    else:
        amount1 = IOUAmount(
            currency=asset1.currency,
            issuer=asset1.issuer,
            value=params.amm_deposit_amount(),
        )
    amount2 = IOUAmount(
        currency=asset2.currency,
        issuer=asset2.issuer,
        value=params.amm_deposit_amount(),
    )
    txn = AMMCreate(
        account=src.address,
        amount=amount1,
        amount2=amount2,
        trading_fee=params.amm_trading_fee(),
    )
    await submit_tx("AMMCreate", txn, client, src.wallet)


def _mpt_xrp_create(account: str, mpt: MPTokenIssuance) -> AMMCreate:
    """A well-formed MPT+XRP AMMCreate model (valid shape; the FAULT is which
    issuance/creator is chosen, applied by rippled preclaim). Submitted via the
    raw path so rippled's own preclaim is what rejects it."""
    return AMMCreate(
        account=account,
        amount=_amount_for(MPTCurrency(mpt_issuance_id=mpt.mpt_issuance_id), _mpt_create_value()),
        amount2=params.amm_xrp_amount(),
        trading_fee=params.amm_trading_fee(),
    )


async def _amm_create_faulty(
    accounts: dict[str, UserAccount],
    amms: list[AMM],
    trust_lines: list[TrustLine],
    mpt_issuances: list[MPTokenIssuance],
    client: AsyncJsonRpcClient,
) -> None:
    if not accounts:
        return
    src = choice(list(accounts.values()))
    nt = _no_trade(mpt_issuances)
    ra = _require_auth(mpt_issuances)
    lk = _locked(mpt_issuances)

    # Existing IOU/XRP vectors stay on submit_tx. MPT vectors ride submit_raw /
    # submit_fuzzed and are only offered when their cohort + a controlled holder
    # exist.
    mutations = [
        "non_existent_asset",
        "same_asset_both",
        "zero_amount",
        "duplicate_amm",
        "unfunded_create",
    ]
    if _tradeable(mpt_issuances):
        mutations.append("fuzz")
    if any(_controlled_holders(m, accounts) for m in nt):
        mutations.append("mpt_no_trade")
    if any(a != m.issuer and a in m.holders for m in ra for a in accounts):
        mutations.append("mpt_require_auth")
    if any(_controlled_holders(m, accounts) for m in lk):
        mutations.append("mpt_locked")
    mutation = choice(mutations)

    if mutation == "fuzz":
        # Generative: corrupt a valid MPT AMMCreate in open-ended ways.
        built = _amm_create_mpt_base(accounts, mpt_issuances)
        if built is None:
            return
        base, creator = built
        await submit_fuzzed("AMMCreate", base, client, creator.wallet)
        return

    if mutation == "mpt_no_trade":
        # CanTrade unset on the MPT leg -> tecNO_PERMISSION. Verified in
        # AMMCreate.cpp:198-203 (canMPTTradeAndTransfer) -> MPTokenHelpers.cpp
        # canTrade:590-591 returns tecNO_PERMISSION when lsfMPTCanTrade is unset.
        # The creator is a controlled holder (funded), so the earlier
        # tecUNFUNDED_AMM balance check (AMMCreate.cpp:171) is passed first.
        usable = [m for m in nt if _controlled_holders(m, accounts)]
        mpt = choice(usable)
        creator = accounts[choice(_controlled_holders(mpt, accounts))]
        base = _mpt_xrp_create(creator.address, mpt)
        await submit_raw("AMMCreate", base, client, creator.wallet)
        return

    if mutation == "mpt_require_auth":
        # require-auth MPT, creator a non-issuer holder never issuer-authorized
        # -> tecNO_AUTH. Verified in AMMCreate.cpp:109-119 (requireAuth on the
        # asset, AuthType::Legacy) -> MPTokenHelpers.cpp requireAuth:394-396
        # (lsfMPTRequireAuth set AND MPToken lacks lsfMPTAuthorized). requireAuth
        # runs before the canTrade/balance checks, so this is the dominant code.
        usable = [m for m in ra if any(a != m.issuer and a in m.holders for a in accounts)]
        mpt = choice(usable)
        holders = [a for a in _controlled_holders(mpt, accounts) if a != mpt.issuer]
        if not holders:
            return
        creator = accounts[choice(holders)]
        base = _mpt_xrp_create(creator.address, mpt)
        await submit_raw("AMMCreate", base, client, creator.wallet)
        return

    if mutation == "mpt_locked":
        # Globally-locked MPT, creator a controlled holder (funded pre-lock)
        # -> tecLOCKED (NOT tecFROZEN). Verified in AMMCreate.cpp:122-132
        # (checkFrozen on the asset) -> TokenHelpers.cpp checkFrozen(MPTIssue):
        # 96-99 returns tecLOCKED for a frozen/locked MPT. requireAuth passes
        # first (locked cohort is not require-auth), then checkFrozen fires.
        usable = [m for m in lk if _controlled_holders(m, accounts)]
        mpt = choice(usable)
        creator = accounts[choice(_controlled_holders(mpt, accounts))]
        base = _mpt_xrp_create(creator.address, mpt)
        await submit_raw("AMMCreate", base, client, creator.wallet)
        return

    if mutation == "non_existent_asset":
        amount1 = params.amm_xrp_amount()
        fake = _fake_iou()
        amount2 = IOUAmount(
            currency=fake.currency,
            issuer=fake.issuer,
            value=params.amm_deposit_amount(),
        )
        txn = AMMCreate(
            account=src.address,
            amount=amount1,
            amount2=amount2,
            trading_fee=params.amm_trading_fee(),
        )

    elif mutation == "same_asset_both":
        if not trust_lines:
            return
        tl = choice(trust_lines)
        iou = IssuedCurrency(currency=tl.currency, issuer=tl.account_b)
        amount = IOUAmount(
            currency=iou.currency,
            issuer=iou.issuer,
            value=params.amm_deposit_amount(),
        )
        txn = AMMCreate(
            account=src.address,
            amount=amount,
            amount2=amount,
            trading_fee=params.amm_trading_fee(),
        )

    elif mutation == "zero_amount":
        fake = _fake_iou()
        amount2 = IOUAmount(currency=fake.currency, issuer=fake.issuer, value="0")
        txn = AMMCreate(
            account=src.address,
            amount="0",
            amount2=amount2,
            trading_fee=params.amm_trading_fee(),
        )

    elif mutation == "duplicate_amm":
        # Try to create an AMM for an asset pair that already exists (tecDUPLICATE).
        # _amount_for handles XRP/IOU/MPT legs so MPT-paired pools are safe here.
        if not amms:
            return
        amm = choice(amms)
        if len(amm.assets) < 2:
            return
        a1, a2 = amm.assets[0], amm.assets[1]
        amount1 = (
            params.amm_xrp_amount()
            if isinstance(a1, xrpl.models.XRP)
            else _amount_for(a1, params.amm_deposit_amount())
        )
        amount2 = (
            params.amm_xrp_amount()
            if isinstance(a2, xrpl.models.XRP)
            else _amount_for(a2, params.amm_deposit_amount())
        )
        txn = AMMCreate(
            account=src.address,
            amount=amount1,
            amount2=amount2,
            trading_fee=params.amm_trading_fee(),
        )

    else:  # unfunded_create
        # Create with assets the account doesn't hold (tecUNFUNDED_AMM)
        fake = _fake_iou()
        amount2 = IOUAmount(
            currency=fake.currency,
            issuer=fake.issuer,
            value=str(randint(1_000_000, 999_999_999)),
        )
        txn = AMMCreate(
            account=src.address,
            amount=str(randint(10_000_000_000, 99_000_000_000)),  # more XRP than account has
            amount2=amount2,
            trading_fee=params.amm_trading_fee(),
        )

    await submit_tx("AMMCreate", txn, client, src.wallet)


# ── AMMDeposit ───────────────────────────────────────────────────────


async def amm_deposit(
    accounts: dict[str, UserAccount],
    amms: list[AMM],
    client: AsyncJsonRpcClient,
) -> None:
    if params.should_send_faulty():
        return await _amm_deposit_faulty(accounts, amms, client)
    return await _amm_deposit_valid(accounts, amms, client)


async def _amm_deposit_valid(
    accounts: dict[str, UserAccount],
    amms: list[AMM],
    client: AsyncJsonRpcClient,
) -> None:
    if not accounts or not amms:
        return
    amm = choice(amms)
    src = choice(list(accounts.values()))
    if len(amm.assets) < 2:
        return
    asset1, asset2 = amm.assets[0], amm.assets[1]
    mode = choice(
        [
            "single_asset",
            "two_asset",
            "lp_token",
            "one_asset_lp_token",
            "limit_lp_token",
            "two_asset_if_empty",
        ]
    )

    if mode == "single_asset":
        a = choice([asset1, asset2])
        amount = _amount_for(a, params.amm_deposit_amount())
        txn = AMMDeposit(
            account=src.address,
            asset=asset1,
            asset2=asset2,
            amount=amount,
            flags=AMMDepositFlag.TF_SINGLE_ASSET,
        )

    elif mode == "two_asset":
        amt1 = _amount_for(asset1, params.amm_deposit_amount())
        amt2 = _amount_for(asset2, params.amm_deposit_amount())
        txn = AMMDeposit(
            account=src.address,
            asset=asset1,
            asset2=asset2,
            amount=amt1,
            amount2=amt2,
            flags=AMMDepositFlag.TF_TWO_ASSET,
        )

    elif mode == "lp_token":
        if not amm.lp_token:
            return
        lp = amm.lp_token[0]
        lp_out = IOUAmount(
            currency=lp.currency,
            issuer=lp.issuer,
            value=params.amm_lp_token_amount(),
        )
        txn = AMMDeposit(
            account=src.address,
            asset=asset1,
            asset2=asset2,
            lp_token_out=lp_out,
            flags=AMMDepositFlag.TF_LP_TOKEN,
        )

    elif mode == "one_asset_lp_token":
        if not amm.lp_token:
            return
        lp = amm.lp_token[0]
        a = choice([asset1, asset2])
        amount = _amount_for(a, params.amm_deposit_amount())
        lp_out = IOUAmount(
            currency=lp.currency,
            issuer=lp.issuer,
            value=params.amm_lp_token_amount(),
        )
        txn = AMMDeposit(
            account=src.address,
            asset=asset1,
            asset2=asset2,
            amount=amount,
            lp_token_out=lp_out,
            flags=AMMDepositFlag.TF_ONE_ASSET_LP_TOKEN,
        )

    elif mode == "limit_lp_token":
        a = choice([asset1, asset2])
        amount = _amount_for(a, params.amm_deposit_amount())
        e_price = _amount_for(a, str(randint(1, 1000)))
        txn = AMMDeposit(
            account=src.address,
            asset=asset1,
            asset2=asset2,
            amount=amount,
            e_price=e_price,
            flags=AMMDepositFlag.TF_LIMIT_LP_TOKEN,
        )

    else:  # two_asset_if_empty
        amt1 = _amount_for(asset1, params.amm_deposit_amount())
        amt2 = _amount_for(asset2, params.amm_deposit_amount())
        txn = AMMDeposit(
            account=src.address,
            asset=asset1,
            asset2=asset2,
            amount=amt1,
            amount2=amt2,
            flags=AMMDepositFlag.TF_TWO_ASSET_IF_EMPTY,
        )

    await submit_tx("AMMDeposit", txn, client, src.wallet)


async def _amm_deposit_faulty(
    accounts: dict[str, UserAccount],
    amms: list[AMM],
    client: AsyncJsonRpcClient,
) -> None:
    if not accounts:
        return
    src = choice(list(accounts.values()))
    mutation = choice(
        [
            "non_existent_amm",
            "zero_deposit",
            "wrong_asset",
            "mismatched_flag_fields",
            "negative_amount",
        ]
    )

    if mutation == "non_existent_amm":
        fake = _fake_iou()
        amount = IOUAmount(
            currency=fake.currency,
            issuer=fake.issuer,
            value=params.amm_deposit_amount(),
        )
        txn = AMMDeposit(
            account=src.address,
            asset=xrpl.models.XRP(),
            asset2=fake,
            amount=amount,
            flags=AMMDepositFlag.TF_SINGLE_ASSET,
        )

    elif mutation == "zero_deposit":
        if not amms:
            return
        amm = choice(amms)
        asset = amm.assets[0] if amm.assets else None
        if not asset:
            return
        amount = _amount_for(asset, "0")
        txn = AMMDeposit(
            account=src.address,
            asset=amm.assets[0],
            asset2=amm.assets[1] if len(amm.assets) > 1 else None,
            amount=amount,
            flags=AMMDepositFlag.TF_SINGLE_ASSET,
        )

    elif mutation == "wrong_asset":
        if not amms:
            return
        amm = choice(amms)
        fake = _fake_iou()
        amount = IOUAmount(
            currency=fake.currency,
            issuer=fake.issuer,
            value=params.amm_deposit_amount(),
        )
        txn = AMMDeposit(
            account=src.address,
            asset=amm.assets[0],
            asset2=amm.assets[1] if len(amm.assets) > 1 else None,
            amount=amount,
            flags=AMMDepositFlag.TF_SINGLE_ASSET,
        )

    elif mutation == "mismatched_flag_fields":
        # TF_TWO_ASSET but only provide one amount — flag/field mismatch
        if not amms:
            return
        amm = choice(amms)
        if len(amm.assets) < 2:
            return
        amount = _amount_for(amm.assets[0], params.amm_deposit_amount())
        txn = AMMDeposit(
            account=src.address,
            asset=amm.assets[0],
            asset2=amm.assets[1],
            amount=amount,
            flags=AMMDepositFlag.TF_TWO_ASSET,
        )

    else:  # negative_amount
        if not amms:
            return
        amm = choice(amms)
        if len(amm.assets) < 2:
            return
        # Signed-IOU amount: needs an IOU leg (XRP rejects negative drops at the
        # codec; xrpl-py has no negative MPTAmount model) — skip MPT-only pools.
        asset = _iou_leg(amm)
        if asset is None:
            return
        amount = IOUAmount(currency=asset.currency, issuer=asset.issuer, value="-1")
        txn = AMMDeposit(
            account=src.address,
            asset=amm.assets[0],
            asset2=amm.assets[1],
            amount=amount,
            flags=AMMDepositFlag.TF_SINGLE_ASSET,
        )

    await submit_tx("AMMDeposit", txn, client, src.wallet)


# ── AMMWithdraw ──────────────────────────────────────────────────────


async def amm_withdraw(
    accounts: dict[str, UserAccount],
    amms: list[AMM],
    client: AsyncJsonRpcClient,
) -> None:
    if params.should_send_faulty():
        return await _amm_withdraw_faulty(accounts, amms, client)
    return await _amm_withdraw_valid(accounts, amms, client)


async def _amm_withdraw_valid(
    accounts: dict[str, UserAccount],
    amms: list[AMM],
    client: AsyncJsonRpcClient,
) -> None:
    if not accounts or not amms:
        return
    amm = choice(amms)
    src = choice(list(accounts.values()))
    if len(amm.assets) < 2:
        return
    asset1, asset2 = amm.assets[0], amm.assets[1]
    mode = choice(
        [
            "single_asset",
            "lp_token",
            "withdraw_all",
            "one_asset_withdraw_all",
            "two_asset",
            "one_asset_lp_token",
            "limit_lp_token",
        ]
    )

    if mode == "single_asset":
        a = choice([asset1, asset2])
        amount = _amount_for(a, params.amm_withdraw_amount())
        txn = AMMWithdraw(
            account=src.address,
            asset=asset1,
            asset2=asset2,
            amount=amount,
            flags=AMMWithdrawFlag.TF_SINGLE_ASSET,
        )

    elif mode == "lp_token":
        if not amm.lp_token:
            return
        lp = amm.lp_token[0]
        lp_in = IOUAmount(
            currency=lp.currency,
            issuer=lp.issuer,
            value=params.amm_lp_token_amount(),
        )
        txn = AMMWithdraw(
            account=src.address,
            asset=asset1,
            asset2=asset2,
            lp_token_in=lp_in,
            flags=AMMWithdrawFlag.TF_LP_TOKEN,
        )

    elif mode == "withdraw_all":
        txn = AMMWithdraw(
            account=src.address,
            asset=asset1,
            asset2=asset2,
            flags=AMMWithdrawFlag.TF_WITHDRAW_ALL,
        )

    elif mode == "one_asset_withdraw_all":
        a = choice([asset1, asset2])
        amount = _amount_for(a, params.amm_withdraw_amount())
        txn = AMMWithdraw(
            account=src.address,
            asset=asset1,
            asset2=asset2,
            amount=amount,
            flags=AMMWithdrawFlag.TF_ONE_ASSET_WITHDRAW_ALL,
        )

    elif mode == "two_asset":
        amt1 = _amount_for(asset1, params.amm_withdraw_amount())
        amt2 = _amount_for(asset2, params.amm_withdraw_amount())
        txn = AMMWithdraw(
            account=src.address,
            asset=asset1,
            asset2=asset2,
            amount=amt1,
            amount2=amt2,
            flags=AMMWithdrawFlag.TF_TWO_ASSET,
        )

    elif mode == "one_asset_lp_token":
        if not amm.lp_token:
            return
        lp = amm.lp_token[0]
        a = choice([asset1, asset2])
        amount = _amount_for(a, params.amm_withdraw_amount())
        lp_in = IOUAmount(
            currency=lp.currency,
            issuer=lp.issuer,
            value=params.amm_lp_token_amount(),
        )
        txn = AMMWithdraw(
            account=src.address,
            asset=asset1,
            asset2=asset2,
            amount=amount,
            lp_token_in=lp_in,
            flags=AMMWithdrawFlag.TF_ONE_ASSET_LP_TOKEN,
        )

    else:  # limit_lp_token
        a = choice([asset1, asset2])
        amount = _amount_for(a, params.amm_withdraw_amount())
        e_price = _amount_for(a, str(randint(1, 1000)))
        txn = AMMWithdraw(
            account=src.address,
            asset=asset1,
            asset2=asset2,
            amount=amount,
            e_price=e_price,
            flags=AMMWithdrawFlag.TF_LIMIT_LP_TOKEN,
        )

    await submit_tx("AMMWithdraw", txn, client, src.wallet)


async def _amm_withdraw_faulty(
    accounts: dict[str, UserAccount],
    amms: list[AMM],
    client: AsyncJsonRpcClient,
) -> None:
    if not accounts:
        return
    src = choice(list(accounts.values()))
    mutation = choice(
        [
            "non_existent_amm",
            "zero_withdrawal",
            "overdraw",
            "mismatched_flag_fields",
            "negative_amount",
        ]
    )

    if mutation == "non_existent_amm":
        fake = _fake_iou()
        amount = IOUAmount(
            currency=fake.currency,
            issuer=fake.issuer,
            value=params.amm_withdraw_amount(),
        )
        txn = AMMWithdraw(
            account=src.address,
            asset=xrpl.models.XRP(),
            asset2=fake,
            amount=amount,
            flags=AMMWithdrawFlag.TF_SINGLE_ASSET,
        )

    elif mutation == "zero_withdrawal":
        if not amms:
            return
        amm = choice(amms)
        asset = amm.assets[0] if amm.assets else None
        if not asset:
            return
        amount = _amount_for(asset, "0")
        txn = AMMWithdraw(
            account=src.address,
            asset=amm.assets[0],
            asset2=amm.assets[1] if len(amm.assets) > 1 else None,
            amount=amount,
            flags=AMMWithdrawFlag.TF_SINGLE_ASSET,
        )

    elif mutation == "overdraw":
        if not amms:
            return
        amm = choice(amms)
        if len(amm.assets) < 2:
            return
        # Far-overshoot IOU value (within IOU precision, beyond any pool balance);
        # needs an IOU leg — skip MPT-only pools (10**15 also overflows uint64).
        asset = _iou_leg(amm)
        if asset is None:
            return
        amount = IOUAmount(
            currency=asset.currency,
            issuer=asset.issuer,
            value=str(10**15),  # within IOU precision but far exceeds pool balance
        )
        txn = AMMWithdraw(
            account=src.address,
            asset=amm.assets[0],
            asset2=amm.assets[1],
            amount=amount,
            flags=AMMWithdrawFlag.TF_SINGLE_ASSET,
        )

    elif mutation == "mismatched_flag_fields":
        # TF_TWO_ASSET but only provide one amount — flag/field mismatch
        if not amms:
            return
        amm = choice(amms)
        if len(amm.assets) < 2:
            return
        amount = _amount_for(amm.assets[0], params.amm_withdraw_amount())
        txn = AMMWithdraw(
            account=src.address,
            asset=amm.assets[0],
            asset2=amm.assets[1],
            amount=amount,
            flags=AMMWithdrawFlag.TF_TWO_ASSET,
        )

    else:  # negative_amount
        if not amms:
            return
        amm = choice(amms)
        if len(amm.assets) < 2:
            return
        # Signed-IOU amount: needs an IOU leg (XRP rejects negative drops at the
        # codec; xrpl-py has no negative MPTAmount model) — skip MPT-only pools.
        asset = _iou_leg(amm)
        if asset is None:
            return
        amount = IOUAmount(currency=asset.currency, issuer=asset.issuer, value="-1")
        txn = AMMWithdraw(
            account=src.address,
            asset=amm.assets[0],
            asset2=amm.assets[1],
            amount=amount,
            flags=AMMWithdrawFlag.TF_SINGLE_ASSET,
        )

    await submit_tx("AMMWithdraw", txn, client, src.wallet)


# ── AMMVote ──────────────────────────────────────────────────────────


async def amm_vote(
    accounts: dict[str, UserAccount],
    amms: list[AMM],
    trust_lines: list[TrustLine],
    client: AsyncJsonRpcClient,
) -> None:
    if params.should_send_faulty():
        return await _amm_vote_faulty(accounts, amms, client)
    return await _amm_vote_valid(accounts, amms, trust_lines, client)


async def _amm_vote_valid(
    accounts: dict[str, UserAccount],
    amms: list[AMM],
    trust_lines: list[TrustLine],
    client: AsyncJsonRpcClient,
) -> None:
    if not accounts or not amms:
        return
    amm = choice(amms)
    # Pick an account that holds the AMM's IOU assets (likely an LP token holder).
    # Only IOU legs need a trust line; MPT/XRP legs are filtered out so an
    # MPT-paired pool does not feed an MPTCurrency into the trust-line matcher.
    needed = [a for a in amm.assets if isinstance(a, IssuedCurrency)]
    src = _find_account_with_trust_lines(accounts, trust_lines, needed)
    if not src:
        return
    txn = AMMVote(
        account=src.address,
        asset=amm.assets[0],
        asset2=amm.assets[1] if len(amm.assets) > 1 else None,
        trading_fee=params.amm_vote_fee(),
    )
    await submit_tx("AMMVote", txn, client, src.wallet)


async def _amm_vote_faulty(
    accounts: dict[str, UserAccount],
    amms: list[AMM],
    client: AsyncJsonRpcClient,
) -> None:
    if not accounts:
        return
    src = choice(list(accounts.values()))
    mutation = choice(["non_existent_amm", "non_lp_holder_vote", "swapped_assets"])

    if mutation == "non_existent_amm":
        fake = _fake_iou()
        txn = AMMVote(
            account=src.address,
            asset=xrpl.models.XRP(),
            asset2=fake,
            trading_fee=params.amm_vote_fee(),
        )

    elif mutation == "non_lp_holder_vote":
        # Vote on a real AMM without holding LP tokens (tecAMM_FAILED)
        if not amms:
            return
        amm = choice(amms)
        txn = AMMVote(
            account=src.address,
            asset=amm.assets[0],
            asset2=amm.assets[1] if len(amm.assets) > 1 else None,
            trading_fee=params.amm_vote_fee(),
        )

    else:  # swapped_assets
        # Vote with asset1/asset2 swapped (tecAMM_INVALID_TOKENS)
        if not amms:
            return
        amm = choice(amms)
        if len(amm.assets) < 2:
            return
        txn = AMMVote(
            account=src.address,
            asset=amm.assets[1],
            asset2=amm.assets[0],
            trading_fee=params.amm_vote_fee(),
        )

    await submit_tx("AMMVote", txn, client, src.wallet)


# ── AMMBid ───────────────────────────────────────────────────────────


async def amm_bid(
    accounts: dict[str, UserAccount],
    amms: list[AMM],
    client: AsyncJsonRpcClient,
) -> None:
    if params.should_send_faulty():
        return await _amm_bid_faulty(accounts, amms, client)
    return await _amm_bid_valid(accounts, amms, client)


async def _amm_bid_valid(
    accounts: dict[str, UserAccount],
    amms: list[AMM],
    client: AsyncJsonRpcClient,
) -> None:
    if not accounts or not amms:
        return
    amm = choice(amms)
    if not amm.lp_token or len(amm.assets) < 2:
        return
    src = choice(list(accounts.values()))
    lp = amm.lp_token[0]
    mode = choice(["basic_bid", "bid_with_auth_accounts"])

    if mode == "basic_bid":
        bid_min = IOUAmount(currency=lp.currency, issuer=lp.issuer, value=params.amm_bid_min())
        bid_max = IOUAmount(currency=lp.currency, issuer=lp.issuer, value=params.amm_bid_max())
        txn = AMMBid(
            account=src.address,
            asset=amm.assets[0],
            asset2=amm.assets[1],
            bid_min=bid_min,
            bid_max=bid_max,
        )

    else:  # bid_with_auth_accounts
        acct_list = list(accounts.values())
        num_auth = randint(1, min(4, len(acct_list)))
        auth_accounts = [AuthAccount(account=a.address) for a in sample(acct_list, num_auth)]
        bid_min = IOUAmount(currency=lp.currency, issuer=lp.issuer, value=params.amm_bid_min())
        txn = AMMBid(
            account=src.address,
            asset=amm.assets[0],
            asset2=amm.assets[1],
            bid_min=bid_min,
            auth_accounts=auth_accounts,
        )

    await submit_tx("AMMBid", txn, client, src.wallet)


async def _amm_bid_faulty(
    accounts: dict[str, UserAccount],
    amms: list[AMM],
    client: AsyncJsonRpcClient,
) -> None:
    if not accounts:
        return
    src = choice(list(accounts.values()))
    mutation = choice(
        [
            "non_existent_amm",
            "zero_bid",
            "bid_min_exceeds_max",
            "fake_auth_accounts",
            "non_lp_holder_bid",
        ]
    )

    if mutation == "non_existent_amm":
        fake = _fake_iou()
        bid_amt = IOUAmount(currency=fake.currency, issuer=fake.issuer, value=params.amm_bid_min())
        txn = AMMBid(
            account=src.address,
            asset=xrpl.models.XRP(),
            asset2=fake,
            bid_min=bid_amt,
        )

    elif mutation == "zero_bid":
        if not amms:
            return
        amm = choice(amms)
        if not amm.lp_token:
            return
        lp = amm.lp_token[0]
        bid_amt = IOUAmount(currency=lp.currency, issuer=lp.issuer, value="0")
        txn = AMMBid(
            account=src.address,
            asset=amm.assets[0],
            asset2=amm.assets[1] if len(amm.assets) > 1 else None,
            bid_min=bid_amt,
        )

    elif mutation == "bid_min_exceeds_max":
        if not amms:
            return
        amm = choice(amms)
        if not amm.lp_token or len(amm.assets) < 2:
            return
        lp = amm.lp_token[0]
        bid_min = IOUAmount(currency=lp.currency, issuer=lp.issuer, value=params.amm_bid_max())
        bid_max = IOUAmount(currency=lp.currency, issuer=lp.issuer, value=params.amm_bid_min())
        txn = AMMBid(
            account=src.address,
            asset=amm.assets[0],
            asset2=amm.assets[1],
            bid_min=bid_min,
            bid_max=bid_max,
        )

    elif mutation == "fake_auth_accounts":
        if not amms:
            return
        amm = choice(amms)
        if not amm.lp_token or len(amm.assets) < 2:
            return
        lp = amm.lp_token[0]
        fake_auths = [AuthAccount(account=params.fake_account()) for _ in range(randint(1, 4))]
        bid_amt = IOUAmount(currency=lp.currency, issuer=lp.issuer, value=params.amm_bid_min())
        txn = AMMBid(
            account=src.address,
            asset=amm.assets[0],
            asset2=amm.assets[1],
            bid_min=bid_amt,
            auth_accounts=fake_auths,
        )

    else:  # non_lp_holder_bid
        # Bid on a real AMM without holding LP tokens (tecAMM_INVALID_TOKENS)
        if not amms:
            return
        amm = choice(amms)
        if not amm.lp_token or len(amm.assets) < 2:
            return
        lp = amm.lp_token[0]
        bid_amt = IOUAmount(currency=lp.currency, issuer=lp.issuer, value=params.amm_bid_min())
        txn = AMMBid(
            account=src.address,
            asset=amm.assets[0],
            asset2=amm.assets[1],
            bid_min=bid_amt,
        )

    await submit_tx("AMMBid", txn, client, src.wallet)


# ── AMMDelete ────────────────────────────────────────────────────────


async def amm_delete(
    accounts: dict[str, UserAccount],
    amms: list[AMM],
    client: AsyncJsonRpcClient,
) -> None:
    if params.should_send_faulty():
        return await _amm_delete_faulty(accounts, amms, client)
    return await _amm_delete_valid(accounts, amms, client)


async def _amm_delete_valid(
    accounts: dict[str, UserAccount],
    amms: list[AMM],
    client: AsyncJsonRpcClient,
) -> None:
    if not accounts or not amms:
        return
    amm = choice(amms)
    src = choice(list(accounts.values()))
    txn = AMMDelete(
        account=src.address,
        asset=amm.assets[0],
        asset2=amm.assets[1] if len(amm.assets) > 1 else None,
    )
    await submit_tx("AMMDelete", txn, client, src.wallet)


async def _amm_delete_faulty(
    accounts: dict[str, UserAccount],
    amms: list[AMM],
    client: AsyncJsonRpcClient,
) -> None:
    if not accounts:
        return
    src = choice(list(accounts.values()))
    mutation = choice(["non_existent_amm", "non_empty_amm", "wrong_asset_pair"])

    if mutation == "non_existent_amm":
        fake = _fake_iou()
        txn = AMMDelete(
            account=src.address,
            asset=xrpl.models.XRP(),
            asset2=fake,
        )

    elif mutation == "non_empty_amm":
        if not amms:
            return
        amm = choice(amms)
        txn = AMMDelete(
            account=src.address,
            asset=amm.assets[0],
            asset2=amm.assets[1] if len(amm.assets) > 1 else None,
        )

    else:  # wrong_asset_pair — use real assets but wrong combination
        if not amms:
            return
        amm = choice(amms)
        fake = _fake_iou()
        # Swap one real asset with a fake one
        txn = AMMDelete(
            account=src.address,
            asset=amm.assets[0],
            asset2=fake,
        )

    await submit_tx("AMMDelete", txn, client, src.wallet)

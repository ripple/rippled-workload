"""AMM transaction generators; MPT pools (XLS-82) are handled everywhere, not skipped."""

import xrpl.models
from xrpl.asyncio.clients import AsyncJsonRpcClient
from xrpl.models import AuthAccount, IssuedCurrency
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
    """Trust line implies balance: accounts got IOU distributions at setup."""
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
    """asset1 may be XRP, asset2 always IOU; None when no trust lines exist."""
    if not trust_lines:
        return None
    tl = choice(trust_lines)
    iou = IssuedCurrency(currency=tl.currency, issuer=tl.account_b)
    if random() < 0.3:
        return xrpl.models.XRP(), iou
    other_tls = [t for t in trust_lines if t.currency != tl.currency or t.account_b != tl.account_b]
    if other_tls:
        tl2 = choice(other_tls)
        iou2 = IssuedCurrency(currency=tl2.currency, issuer=tl2.account_b)
        return iou, iou2
    return xrpl.models.XRP(), iou


def _fake_iou() -> IssuedCurrency:
    """IOU with valid format but non-existent issuer."""
    return IssuedCurrency(currency=params.currency_code(), issuer=params.fake_account())


def _amount_for(
    asset: IssuedCurrency | MPTCurrency | xrpl.models.XRP, value: str
) -> IOUAmount | MPTAmount | str:
    """Callers pass integer ``value`` so MPTAmount (integers only) stays well-formed."""
    if isinstance(asset, xrpl.models.XRP):
        return value
    if isinstance(asset, MPTCurrency):
        return MPTAmount(mpt_issuance_id=asset.mpt_issuance_id, value=value)
    return IOUAmount(currency=asset.currency, issuer=asset.issuer, value=value)


def _iou_leg(amm: AMM) -> IssuedCurrency | None:
    """IOU leg (skip XRP/MPT), or None: signed-IOU faulty vectors must skip MPT-only pools."""
    for a in amm.assets:
        if isinstance(a, IssuedCurrency):
            return a
    return None


# ── AMMCreate ────────────────────────────────────────────────────────


def _mpt_create_value() -> str:
    """Modest within the 10000 setup distribution so a controlled holder can always fund it."""
    return params.amm_deposit_amount()


def _amm_create_mpt_base(
    accounts: dict[str, UserAccount],
    mpt_issuances: list[MPTokenIssuance],
) -> tuple[AMMCreate, UserAccount] | None:
    """Valid MPT-paired AMMCreate + creator; None when no tradeable MPT/holder exists."""
    tr = _tradeable(mpt_issuances)
    if not tr or not accounts:
        return None
    mpt1 = choice(tr)
    holders1 = _controlled_holders(mpt1, accounts)
    if not holders1:
        return None
    mpt_amt1 = _amount_for(MPTCurrency(mpt_issuance_id=mpt1.mpt_issuance_id), _mpt_create_value())

    # ~25%: MPT+MPT needs a second issuance with an intersecting controlled-holder set.
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
    # ~35% MPT-paired; same-pair recreates hit tecDUPLICATE; IOU/XRP majority keeps success.
    if _tradeable(mpt_issuances) and random() < 0.35:
        built = _amm_create_mpt_base(accounts, mpt_issuances)
        if built is not None:
            base, mpt_src = built
            await submit_tx("AMMCreate", base, client, mpt_src.wallet)
            return
    if not trust_lines:
        return
    pair = _pick_asset_pair(trust_lines)
    if not pair:
        return
    asset1, asset2 = pair
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
    """Well-formed shape; the fault is the issuance/creator choice, left to rippled preclaim."""
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

    # MPT vectors are only offered when their cohort + a controlled holder exist.
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
        built = _amm_create_mpt_base(accounts, mpt_issuances)
        if built is None:
            return
        base, creator = built
        await submit_fuzzed("AMMCreate", base, client, creator.wallet)
        return

    if mutation == "mpt_no_trade":
        # CanTrade unset -> tecNO_PERMISSION; creator is funded so the balance check passes first.
        usable = [m for m in nt if _controlled_holders(m, accounts)]
        mpt = choice(usable)
        creator = accounts[choice(_controlled_holders(mpt, accounts))]
        base = _mpt_xrp_create(creator.address, mpt)
        await submit_raw("AMMCreate", base, client, creator.wallet)
        return

    if mutation == "mpt_require_auth":
        # require-auth MPT, never-authorized holder -> tecNO_AUTH (runs before canTrade/balance).
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
        # Globally-locked MPT, funded holder -> tecLOCKED not tecFROZEN; checkFrozen after auth.
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


def _amm_deposit_base(
    accounts: dict[str, UserAccount],
    amms: list[AMM],
) -> tuple[AMMDeposit, UserAccount] | None:
    """Valid AMMDeposit + depositor; None when no two-asset AMM exists."""
    if not accounts or not amms:
        return None
    amm = choice(amms)
    src = choice(list(accounts.values()))
    if len(amm.assets) < 2:
        return None
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
            return None
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
            return None
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

    return txn, src


async def _amm_deposit_valid(
    accounts: dict[str, UserAccount],
    amms: list[AMM],
    client: AsyncJsonRpcClient,
) -> None:
    built = _amm_deposit_base(accounts, amms)
    if built is None:
        return
    txn, src = built
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
            "fuzz",
            "non_existent_amm",
            "zero_deposit",
            "wrong_asset",
            "mismatched_flag_fields",
            "negative_amount",
        ]
    )

    if mutation == "fuzz":
        built = _amm_deposit_base(accounts, amms)
        if built is None:
            return
        base, depositor = built
        await submit_fuzzed("AMMDeposit", base, client, depositor.wallet)
        return

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
            asset2=amm.assets[1],
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
            asset2=amm.assets[1],
            amount=amount,
            flags=AMMDepositFlag.TF_SINGLE_ASSET,
        )

    elif mutation == "mismatched_flag_fields":
        # TF_TWO_ASSET with only one amount
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
        # Signed-IOU amount needs an IOU leg (no negative MPT/XRP model) — skip MPT-only pools.
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


def _amm_withdraw_base(
    accounts: dict[str, UserAccount],
    amms: list[AMM],
) -> tuple[AMMWithdraw, UserAccount] | None:
    """Valid AMMWithdraw + withdrawer; None when no two-asset AMM exists."""
    if not accounts or not amms:
        return None
    amm = choice(amms)
    src = choice(list(accounts.values()))
    if len(amm.assets) < 2:
        return None
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
            return None
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
            return None
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

    return txn, src


async def _amm_withdraw_valid(
    accounts: dict[str, UserAccount],
    amms: list[AMM],
    client: AsyncJsonRpcClient,
) -> None:
    built = _amm_withdraw_base(accounts, amms)
    if built is None:
        return
    txn, src = built
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
            "fuzz",
            "non_existent_amm",
            "zero_withdrawal",
            "overdraw",
            "mismatched_flag_fields",
            "negative_amount",
        ]
    )

    if mutation == "fuzz":
        built = _amm_withdraw_base(accounts, amms)
        if built is None:
            return
        base, withdrawer = built
        await submit_fuzzed("AMMWithdraw", base, client, withdrawer.wallet)
        return

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
            asset2=amm.assets[1],
            amount=amount,
            flags=AMMWithdrawFlag.TF_SINGLE_ASSET,
        )

    elif mutation == "overdraw":
        if not amms:
            return
        amm = choice(amms)
        if len(amm.assets) < 2:
            return
        # Needs an IOU leg — skip MPT-only pools (10**15 overflows uint64).
        asset = _iou_leg(amm)
        if asset is None:
            return
        amount = IOUAmount(
            currency=asset.currency,
            issuer=asset.issuer,
            value=str(10**15),
        )
        txn = AMMWithdraw(
            account=src.address,
            asset=amm.assets[0],
            asset2=amm.assets[1],
            amount=amount,
            flags=AMMWithdrawFlag.TF_SINGLE_ASSET,
        )

    elif mutation == "mismatched_flag_fields":
        # TF_TWO_ASSET with only one amount
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
        # Signed-IOU amount needs an IOU leg (no negative MPT/XRP model) — skip MPT-only pools.
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
        return await _amm_vote_faulty(accounts, amms, trust_lines, client)
    return await _amm_vote_valid(accounts, amms, trust_lines, client)


def _amm_vote_base(
    accounts: dict[str, UserAccount],
    amms: list[AMM],
    trust_lines: list[TrustLine],
) -> tuple[AMMVote, UserAccount] | None:
    """Valid AMMVote + voter holding the needed trust lines; None when none qualifies."""
    if not accounts or not amms:
        return None
    amm = choice(amms)
    # Filter to IOU legs: only those need a trust line, and MPTCurrency must not reach the matcher.
    needed = [a for a in amm.assets if isinstance(a, IssuedCurrency)]
    src = _find_account_with_trust_lines(accounts, trust_lines, needed)
    if not src:
        return None
    txn = AMMVote(
        account=src.address,
        asset=amm.assets[0],
        asset2=amm.assets[1],
        trading_fee=params.amm_vote_fee(),
    )
    return txn, src


async def _amm_vote_valid(
    accounts: dict[str, UserAccount],
    amms: list[AMM],
    trust_lines: list[TrustLine],
    client: AsyncJsonRpcClient,
) -> None:
    built = _amm_vote_base(accounts, amms, trust_lines)
    if built is None:
        return
    txn, src = built
    await submit_tx("AMMVote", txn, client, src.wallet)


async def _amm_vote_faulty(
    accounts: dict[str, UserAccount],
    amms: list[AMM],
    trust_lines: list[TrustLine],
    client: AsyncJsonRpcClient,
) -> None:
    if not accounts:
        return
    src = choice(list(accounts.values()))
    mutation = choice(["fuzz", "non_existent_amm", "non_lp_holder_vote", "swapped_assets"])

    if mutation == "fuzz":
        built = _amm_vote_base(accounts, amms, trust_lines)
        if built is None:
            return
        base, voter = built
        await submit_fuzzed("AMMVote", base, client, voter.wallet)
        return

    if mutation == "non_existent_amm":
        fake = _fake_iou()
        txn = AMMVote(
            account=src.address,
            asset=xrpl.models.XRP(),
            asset2=fake,
            trading_fee=params.amm_vote_fee(),
        )

    elif mutation == "non_lp_holder_vote":
        # No LP tokens -> tecAMM_FAILED
        if not amms:
            return
        amm = choice(amms)
        txn = AMMVote(
            account=src.address,
            asset=amm.assets[0],
            asset2=amm.assets[1],
            trading_fee=params.amm_vote_fee(),
        )

    else:  # swapped_assets
        # asset1/asset2 swapped -> tecAMM_INVALID_TOKENS
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


def _amm_bid_base(
    accounts: dict[str, UserAccount],
    amms: list[AMM],
) -> tuple[AMMBid, UserAccount] | None:
    """Valid AMMBid + bidder; None when no two-asset AMM with LP tokens exists."""
    if not accounts or not amms:
        return None
    amm = choice(amms)
    if not amm.lp_token or len(amm.assets) < 2:
        return None
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

    return txn, src


async def _amm_bid_valid(
    accounts: dict[str, UserAccount],
    amms: list[AMM],
    client: AsyncJsonRpcClient,
) -> None:
    built = _amm_bid_base(accounts, amms)
    if built is None:
        return
    txn, src = built
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
            "fuzz",
            "non_existent_amm",
            "zero_bid",
            "bid_min_exceeds_max",
            "fake_auth_accounts",
            "non_lp_holder_bid",
        ]
    )

    if mutation == "fuzz":
        built = _amm_bid_base(accounts, amms)
        if built is None:
            return
        base, bidder = built
        await submit_fuzzed("AMMBid", base, client, bidder.wallet)
        return

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
            asset2=amm.assets[1],
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
        # No LP tokens -> tecAMM_INVALID_TOKENS
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


def _amm_delete_base(
    accounts: dict[str, UserAccount],
    amms: list[AMM],
) -> tuple[AMMDelete, UserAccount] | None:
    """Valid AMMDelete + submitter; None when no two-asset AMM exists."""
    if not accounts or not amms:
        return None
    amm = choice(amms)
    if len(amm.assets) < 2:
        return None
    src = choice(list(accounts.values()))
    txn = AMMDelete(
        account=src.address,
        asset=amm.assets[0],
        asset2=amm.assets[1],
    )
    return txn, src


async def _amm_delete_valid(
    accounts: dict[str, UserAccount],
    amms: list[AMM],
    client: AsyncJsonRpcClient,
) -> None:
    built = _amm_delete_base(accounts, amms)
    if built is None:
        return
    txn, src = built
    await submit_tx("AMMDelete", txn, client, src.wallet)


async def _amm_delete_faulty(
    accounts: dict[str, UserAccount],
    amms: list[AMM],
    client: AsyncJsonRpcClient,
) -> None:
    if not accounts:
        return
    src = choice(list(accounts.values()))
    mutation = choice(["fuzz", "non_existent_amm", "non_empty_amm", "wrong_asset_pair"])

    if mutation == "fuzz":
        built = _amm_delete_base(accounts, amms)
        if built is None:
            return
        base, submitter = built
        await submit_fuzzed("AMMDelete", base, client, submitter.wallet)
        return

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
            asset2=amm.assets[1],
        )

    else:  # wrong_asset_pair
        if not amms:
            return
        amm = choice(amms)
        fake = _fake_iou()
        txn = AMMDelete(
            account=src.address,
            asset=amm.assets[0],
            asset2=fake,
        )

    await submit_tx("AMMDelete", txn, client, src.wallet)

"""AMM transaction generators for the antithesis workload."""

import xrpl.models
from xrpl.asyncio.clients import AsyncJsonRpcClient
from xrpl.models import IssuedCurrency
from xrpl.models import IssuedCurrencyAmount as IOUAmount
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
from workload.models import AMM, TrustLine, UserAccount
from workload.randoms import choice, randint, random, sample
from workload.submit import submit_tx


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


def _iou_amount(asset: IssuedCurrency | xrpl.models.XRP, value: str) -> IOUAmount | str:
    """Build an amount for the given asset. Returns drops string for XRP, IOUAmount for IOU."""
    if isinstance(asset, xrpl.models.XRP):
        return value
    return IOUAmount(currency=asset.currency, issuer=asset.issuer, value=value)


# ── AMMCreate ────────────────────────────────────────────────────────


async def amm_create(
    accounts: dict[str, UserAccount],
    amms: list[AMM],
    trust_lines: list[TrustLine],
    client: AsyncJsonRpcClient,
) -> None:
    if params.should_send_faulty():
        return await _amm_create_faulty(accounts, amms, trust_lines, client)
    return await _amm_create_valid(accounts, amms, trust_lines, client)


async def _amm_create_valid(
    accounts: dict[str, UserAccount],
    amms: list[AMM],
    trust_lines: list[TrustLine],
    client: AsyncJsonRpcClient,
) -> None:
    if not accounts or not trust_lines:
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
        amount1 = IOUAmount(currency=asset1.currency, issuer=asset1.issuer, value=params.amm_deposit_amount())
    amount2 = IOUAmount(currency=asset2.currency, issuer=asset2.issuer, value=params.amm_deposit_amount())
    txn = AMMCreate(
        account=src.address,
        amount=amount1,
        amount2=amount2,
        trading_fee=params.amm_trading_fee(),
    )
    await submit_tx("AMMCreate", txn, client, src.wallet)


async def _amm_create_faulty(
    accounts: dict[str, UserAccount],
    amms: list[AMM],
    trust_lines: list[TrustLine],
    client: AsyncJsonRpcClient,
) -> None:
    if not accounts:
        return
    src = choice(list(accounts.values()))
    mutation = choice([
        "non_existent_asset", "same_asset_both",
        "zero_amount", "duplicate_amm", "unfunded_create",
    ])

    if mutation == "non_existent_asset":
        amount1 = params.amm_xrp_amount()
        fake = _fake_iou()
        amount2 = IOUAmount(currency=fake.currency, issuer=fake.issuer, value=params.amm_deposit_amount())
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
        amount = IOUAmount(currency=iou.currency, issuer=iou.issuer, value=params.amm_deposit_amount())
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
        # Try to create an AMM for an asset pair that already exists (tecDUPLICATE)
        if not amms:
            return
        amm = choice(amms)
        if len(amm.assets) < 2:
            return
        a1, a2 = amm.assets[0], amm.assets[1]
        if isinstance(a1, xrpl.models.XRP):
            amount1 = params.amm_xrp_amount()
        else:
            amount1 = IOUAmount(currency=a1.currency, issuer=a1.issuer, value=params.amm_deposit_amount())
        if isinstance(a2, xrpl.models.XRP):
            amount2 = params.amm_xrp_amount()
        else:
            amount2 = IOUAmount(currency=a2.currency, issuer=a2.issuer, value=params.amm_deposit_amount())
        txn = AMMCreate(
            account=src.address,
            amount=amount1,
            amount2=amount2,
            trading_fee=params.amm_trading_fee(),
        )

    else:  # unfunded_create
        # Create with assets the account doesn't hold (tecUNFUNDED_AMM)
        fake = _fake_iou()
        amount2 = IOUAmount(currency=fake.currency, issuer=fake.issuer, value=str(randint(1_000_000, 999_999_999)))
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
    mode = choice([
        "single_asset", "two_asset", "lp_token",
        "one_asset_lp_token", "limit_lp_token", "two_asset_if_empty",
    ])

    if mode == "single_asset":
        a = choice([asset1, asset2])
        amount = _iou_amount(a, params.amm_deposit_amount())
        txn = AMMDeposit(
            account=src.address, asset=asset1, asset2=asset2,
            amount=amount, flags=AMMDepositFlag.TF_SINGLE_ASSET,
        )

    elif mode == "two_asset":
        amt1 = _iou_amount(asset1, params.amm_deposit_amount())
        amt2 = _iou_amount(asset2, params.amm_deposit_amount())
        txn = AMMDeposit(
            account=src.address, asset=asset1, asset2=asset2,
            amount=amt1, amount2=amt2, flags=AMMDepositFlag.TF_TWO_ASSET,
        )

    elif mode == "lp_token":
        if not amm.lp_token:
            return
        lp = amm.lp_token[0]
        lp_out = IOUAmount(currency=lp.currency, issuer=lp.issuer, value=params.amm_lp_token_amount())
        txn = AMMDeposit(
            account=src.address, asset=asset1, asset2=asset2,
            lp_token_out=lp_out, flags=AMMDepositFlag.TF_LP_TOKEN,
        )

    elif mode == "one_asset_lp_token":
        if not amm.lp_token:
            return
        lp = amm.lp_token[0]
        a = choice([asset1, asset2])
        amount = _iou_amount(a, params.amm_deposit_amount())
        lp_out = IOUAmount(currency=lp.currency, issuer=lp.issuer, value=params.amm_lp_token_amount())
        txn = AMMDeposit(
            account=src.address, asset=asset1, asset2=asset2,
            amount=amount, lp_token_out=lp_out,
            flags=AMMDepositFlag.TF_ONE_ASSET_LP_TOKEN,
        )

    elif mode == "limit_lp_token":
        a = choice([asset1, asset2])
        amount = _iou_amount(a, params.amm_deposit_amount())
        e_price = _iou_amount(a, str(randint(1, 1000)))
        txn = AMMDeposit(
            account=src.address, asset=asset1, asset2=asset2,
            amount=amount, e_price=e_price,
            flags=AMMDepositFlag.TF_LIMIT_LP_TOKEN,
        )

    else:  # two_asset_if_empty
        amt1 = _iou_amount(asset1, params.amm_deposit_amount())
        amt2 = _iou_amount(asset2, params.amm_deposit_amount())
        txn = AMMDeposit(
            account=src.address, asset=asset1, asset2=asset2,
            amount=amt1, amount2=amt2,
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
    mutation = choice([
        "non_existent_amm", "zero_deposit", "wrong_asset",
        "mismatched_flag_fields", "negative_amount",
    ])

    if mutation == "non_existent_amm":
        fake = _fake_iou()
        amount = IOUAmount(currency=fake.currency, issuer=fake.issuer, value=params.amm_deposit_amount())
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
        amount = _iou_amount(asset, "0")
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
        amount = IOUAmount(currency=fake.currency, issuer=fake.issuer, value=params.amm_deposit_amount())
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
        amount = _iou_amount(amm.assets[0], params.amm_deposit_amount())
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
        # Use IOU asset to avoid XRP codec rejection of negative drops
        asset = amm.assets[1] if isinstance(amm.assets[0], xrpl.models.XRP) else amm.assets[0]
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
    mode = choice([
        "single_asset", "lp_token", "withdraw_all",
        "one_asset_withdraw_all", "two_asset",
        "one_asset_lp_token", "limit_lp_token",
    ])

    if mode == "single_asset":
        a = choice([asset1, asset2])
        amount = _iou_amount(a, params.amm_withdraw_amount())
        txn = AMMWithdraw(
            account=src.address, asset=asset1, asset2=asset2,
            amount=amount, flags=AMMWithdrawFlag.TF_SINGLE_ASSET,
        )

    elif mode == "lp_token":
        if not amm.lp_token:
            return
        lp = amm.lp_token[0]
        lp_in = IOUAmount(currency=lp.currency, issuer=lp.issuer, value=params.amm_lp_token_amount())
        txn = AMMWithdraw(
            account=src.address, asset=asset1, asset2=asset2,
            lp_token_in=lp_in, flags=AMMWithdrawFlag.TF_LP_TOKEN,
        )

    elif mode == "withdraw_all":
        txn = AMMWithdraw(
            account=src.address, asset=asset1, asset2=asset2,
            flags=AMMWithdrawFlag.TF_WITHDRAW_ALL,
        )

    elif mode == "one_asset_withdraw_all":
        a = choice([asset1, asset2])
        amount = _iou_amount(a, params.amm_withdraw_amount())
        txn = AMMWithdraw(
            account=src.address, asset=asset1, asset2=asset2,
            amount=amount, flags=AMMWithdrawFlag.TF_ONE_ASSET_WITHDRAW_ALL,
        )

    elif mode == "two_asset":
        amt1 = _iou_amount(asset1, params.amm_withdraw_amount())
        amt2 = _iou_amount(asset2, params.amm_withdraw_amount())
        txn = AMMWithdraw(
            account=src.address, asset=asset1, asset2=asset2,
            amount=amt1, amount2=amt2, flags=AMMWithdrawFlag.TF_TWO_ASSET,
        )

    elif mode == "one_asset_lp_token":
        if not amm.lp_token:
            return
        lp = amm.lp_token[0]
        a = choice([asset1, asset2])
        amount = _iou_amount(a, params.amm_withdraw_amount())
        lp_in = IOUAmount(currency=lp.currency, issuer=lp.issuer, value=params.amm_lp_token_amount())
        txn = AMMWithdraw(
            account=src.address, asset=asset1, asset2=asset2,
            amount=amount, lp_token_in=lp_in,
            flags=AMMWithdrawFlag.TF_ONE_ASSET_LP_TOKEN,
        )

    else:  # limit_lp_token
        a = choice([asset1, asset2])
        amount = _iou_amount(a, params.amm_withdraw_amount())
        e_price = _iou_amount(a, str(randint(1, 1000)))
        txn = AMMWithdraw(
            account=src.address, asset=asset1, asset2=asset2,
            amount=amount, e_price=e_price,
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
    mutation = choice([
        "non_existent_amm", "zero_withdrawal", "overdraw",
        "mismatched_flag_fields", "negative_amount",
    ])

    if mutation == "non_existent_amm":
        fake = _fake_iou()
        amount = IOUAmount(currency=fake.currency, issuer=fake.issuer, value=params.amm_withdraw_amount())
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
        amount = _iou_amount(asset, "0")
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
        # Use IOU asset to avoid XRP issuer issue
        asset = amm.assets[1] if isinstance(amm.assets[0], xrpl.models.XRP) else amm.assets[0]
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
        amount = _iou_amount(amm.assets[0], params.amm_withdraw_amount())
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
        # Use IOU asset to avoid XRP codec rejection of negative drops
        asset = amm.assets[1] if isinstance(amm.assets[0], xrpl.models.XRP) else amm.assets[0]
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
    # Pick account that holds the AMM's IOU assets (likely LP token holder)
    needed = [a for a in amm.assets if not isinstance(a, xrpl.models.XRP)]
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
            asset=amm.assets[0], asset2=amm.assets[1],
            bid_min=bid_min, bid_max=bid_max,
        )

    else:  # bid_with_auth_accounts
        acct_list = list(accounts.values())
        num_auth = randint(1, min(4, len(acct_list)))
        auth_accounts = [AuthAccount(account=a.address) for a in sample(acct_list, num_auth)]
        bid_min = IOUAmount(currency=lp.currency, issuer=lp.issuer, value=params.amm_bid_min())
        txn = AMMBid(
            account=src.address,
            asset=amm.assets[0], asset2=amm.assets[1],
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
    mutation = choice([
        "non_existent_amm", "zero_bid",
        "bid_min_exceeds_max", "fake_auth_accounts", "non_lp_holder_bid",
    ])

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
            asset=amm.assets[0], asset2=amm.assets[1],
            bid_min=bid_min, bid_max=bid_max,
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
            asset=amm.assets[0], asset2=amm.assets[1],
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
            asset=amm.assets[0], asset2=amm.assets[1],
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
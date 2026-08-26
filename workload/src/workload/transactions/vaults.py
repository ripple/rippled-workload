"""Vault transaction generators for the antithesis workload."""

import xrpl.models
from xrpl.asyncio.clients import AsyncJsonRpcClient
from xrpl.models import IssuedCurrency
from xrpl.models import IssuedCurrencyAmount as IOUAmount
from xrpl.models.amounts import MPTAmount
from xrpl.models.currencies import MPTCurrency
from xrpl.models.transactions import (
    VaultClawback,
    VaultDeposit,
    VaultSet,
    VaultWithdraw,
)
from xrpl.wallet import Wallet

from workload import lending_v1_1_compat as lv
from workload import params
from workload.fuzz import submit_fuzzed

# VaultCreate and VaultDelete ride the compat shim until xrpl-py carries the
# XLS-65 closed-ended fields and the V1.1 MemoData; see lending_v1_1_compat.
from workload.lending_v1_1_compat import VaultCreate, VaultDelete
from workload.models import MPTokenIssuance, TrustLine, UserAccount, Vault
from workload.randoms import choice, randint, random
from workload.submit import submit_tx

# Ledger close time trails wall clock, so a vault whose phase would change
# within this many seconds is treated as being in both phases.
_PHASE_SKEW = 60
# Phases in which rippled rejects the transaction (VaultDeposit tecEXPIRED,
# VaultWithdraw tecTOO_SOON).
_DEPOSIT_BLOCKED = frozenset({lv.VaultPhase.INVESTMENT, lv.VaultPhase.REDEMPTION})
_WITHDRAW_BLOCKED = frozenset({lv.VaultPhase.INVESTMENT})


def _phases_around(vault: Vault) -> set[lv.VaultPhase]:
    now = params._ripple_now()
    return {
        lv.vault_phase(vault.vault_kind, vault.subscription_date, vault.redemption_date, t)
        for t in (now - _PHASE_SKEW, now + _PHASE_SKEW)
    }


def _phase_permits(vault: Vault, blocked: frozenset[lv.VaultPhase]) -> bool:
    return not (_phases_around(vault) & blocked)


def _phase_blocks(vault: Vault, blocked: frozenset[lv.VaultPhase]) -> bool:
    return _phases_around(vault) <= blocked


def _pick_permitted_vault(vaults: list[Vault], blocked: frozenset[lv.VaultPhase]) -> Vault | None:
    eligible = [v for v in vaults if _phase_permits(v, blocked)]
    return choice(eligible) if eligible else None


def _pick_blocked_vault(vaults: list[Vault], blocked: frozenset[lv.VaultPhase]) -> Vault | None:
    eligible = [v for v in vaults if _phase_blocks(v, blocked)]
    return choice(eligible) if eligible else None


# ── Create ───────────────────────────────────────────────────────────


def _amount_for_asset(asset: object) -> IOUAmount | MPTAmount | str:
    if isinstance(asset, IssuedCurrency):
        return IOUAmount(
            currency=asset.currency,
            issuer=asset.issuer,
            value=params.iou_amount(),
        )
    elif isinstance(asset, MPTCurrency):
        return MPTAmount(
            mpt_issuance_id=asset.mpt_issuance_id,
            value=params.mpt_amount(),
        )
    return params.vault_deposit_amount()


def _clawback_amount(asset: object) -> IOUAmount | MPTAmount | None:
    """VaultClawback amount — IOU/MPT only (XRP vaults can't be clawed back)."""
    amount = _amount_for_asset(asset)
    return None if isinstance(amount, str) else amount


def _random_asset(
    trust_lines: list[TrustLine], mpt_issuances: list[MPTokenIssuance]
) -> IssuedCurrency | MPTCurrency | xrpl.models.XRP:
    roll = random()
    if trust_lines and roll < 0.33:
        tl = choice(trust_lines)
        issuer = choice([tl.account_a, tl.account_b])
        return IssuedCurrency(currency=tl.currency, issuer=issuer)
    elif mpt_issuances and roll < 0.66:
        mpt = choice(mpt_issuances)
        return MPTCurrency(mpt_issuance_id=mpt.mpt_issuance_id)
    return xrpl.models.XRP()


async def vault_create(
    accounts: dict[str, UserAccount],
    vaults: list[Vault],
    trust_lines: list[TrustLine],
    mpt_issuances: list[MPTokenIssuance],
    client: AsyncJsonRpcClient,
) -> None:
    if params.should_send_faulty():
        return await _vault_create_faulty(accounts, vaults, trust_lines, mpt_issuances, client)
    return await _vault_create_valid(accounts, vaults, trust_lines, mpt_issuances, client)


def _vault_create_base(
    accounts: dict[str, UserAccount],
    trust_lines: list[TrustLine],
    mpt_issuances: list[MPTokenIssuance],
) -> tuple[VaultCreate, Wallet] | None:
    if not accounts:
        return None
    src = accounts[choice(list(accounts))]
    asset = _random_asset(trust_lines, mpt_issuances)
    if lv.enabled() and params.should_create_closed_ended_vault():
        sub, red = params.closed_ended_dates()
        txn = VaultCreate(
            account=src.address,
            asset=asset,
            assets_maximum=params.vault_assets_maximum(),
            data=params.vault_data(),
            vault_kind=int(lv.VaultKind.CLOSED_ENDED),
            subscription_date=sub,
            redemption_date=red,
        )
    else:
        txn = VaultCreate(
            account=src.address,
            asset=asset,
            assets_maximum=params.vault_assets_maximum(),
            data=params.vault_data(),
        )
    return txn, src.wallet


async def _vault_create_valid(
    accounts: dict[str, UserAccount],
    vaults: list[Vault],
    trust_lines: list[TrustLine],
    mpt_issuances: list[MPTokenIssuance],
    client: AsyncJsonRpcClient,
) -> None:
    built = _vault_create_base(accounts, trust_lines, mpt_issuances)
    if built is None:
        return
    txn, wallet = built
    await submit_tx("VaultCreate", txn, client, wallet)


async def _vault_create_faulty(
    accounts: dict[str, UserAccount],
    vaults: list[Vault],
    trust_lines: list[TrustLine],
    mpt_issuances: list[MPTokenIssuance],
    client: AsyncJsonRpcClient,
) -> None:
    if not accounts:
        return
    src = choice(list(accounts.values()))
    asset = _random_asset(trust_lines, mpt_issuances)
    mutations = ["fuzz", "zero_max", "oversized_data", "xrp_with_issuer"]
    if lv.enabled():
        # temDISABLED without the amendment, so these only join once a validated
        # Vault proved it active.
        mutations += ["short_gap", "expired_dates", "invalid_kind"]
    mutation = choice(mutations)
    if mutation == "fuzz":
        built = _vault_create_base(accounts, trust_lines, mpt_issuances)
        if built is None:
            return
        base, wallet = built
        await submit_fuzzed("VaultCreate", base, client, wallet)
        return
    if mutation == "zero_max":
        txn = VaultCreate(
            account=src.address,
            asset=asset,
            assets_maximum="0",
            data=params.vault_data(),
        )
    elif mutation == "oversized_data":
        oversized = bytes(randint(0, 255) for _ in range(513)).hex()
        txn = VaultCreate(
            account=src.address,
            asset=asset,
            assets_maximum=params.vault_assets_maximum(),
            data=oversized,
        )
    elif mutation == "xrp_with_issuer":
        bad_asset = IssuedCurrency(
            currency="XRP",
            issuer=choice(list(accounts.values())).address,
        )
        txn = VaultCreate(
            account=src.address,
            asset=bad_asset,
            assets_maximum=params.vault_assets_maximum(),
            data=params.vault_data(),
        )
    elif mutation == "short_gap":
        sub, red = params.closed_ended_short_gap()
        txn = VaultCreate(
            account=src.address,
            asset=asset,
            assets_maximum=params.vault_assets_maximum(),
            data=params.vault_data(),
            vault_kind=int(lv.VaultKind.CLOSED_ENDED),
            subscription_date=sub,
            redemption_date=red,
        )
    elif mutation == "expired_dates":
        sub, red = params.closed_ended_expired_dates()
        txn = VaultCreate(
            account=src.address,
            asset=asset,
            assets_maximum=params.vault_assets_maximum(),
            data=params.vault_data(),
            vault_kind=int(lv.VaultKind.CLOSED_ENDED),
            subscription_date=sub,
            redemption_date=red,
        )
    else:  # invalid_kind — dates stay well-formed so the kind is the only fault
        sub, red = params.closed_ended_dates()
        txn = VaultCreate(
            account=src.address,
            asset=asset,
            assets_maximum=params.vault_assets_maximum(),
            data=params.vault_data(),
            vault_kind=params.invalid_vault_kind(),
            subscription_date=sub,
            redemption_date=red,
        )
    await submit_tx("VaultCreate", txn, client, src.wallet)


# ── Deposit ──────────────────────────────────────────────────────────


async def vault_deposit(
    accounts: dict[str, UserAccount], vaults: list[Vault], client: AsyncJsonRpcClient
) -> None:
    if params.should_send_faulty():
        return await _vault_deposit_faulty(accounts, vaults, client)
    return await _vault_deposit_valid(accounts, vaults, client)


def _vault_deposit_base(
    accounts: dict[str, UserAccount], vaults: list[Vault]
) -> tuple[VaultDeposit, Wallet] | None:
    if not vaults or not accounts:
        return None
    vault = _pick_permitted_vault(vaults, _DEPOSIT_BLOCKED)
    if vault is None:
        return None
    depositor = accounts[choice(list(accounts))]
    txn = VaultDeposit(
        account=depositor.address,
        vault_id=vault.vault_id,
        amount=_amount_for_asset(vault.asset),
    )
    return txn, depositor.wallet


async def _vault_deposit_valid(
    accounts: dict[str, UserAccount], vaults: list[Vault], client: AsyncJsonRpcClient
) -> None:
    built = _vault_deposit_base(accounts, vaults)
    if built is None:
        return
    txn, wallet = built
    await submit_tx("VaultDeposit", txn, client, wallet)


async def _vault_deposit_faulty(
    accounts: dict[str, UserAccount], vaults: list[Vault], client: AsyncJsonRpcClient
) -> None:
    if not accounts:
        return
    depositor = choice(list(accounts.values()))
    mutations = ["fuzz", "fake_vault", "zero_amount", "mismatched_asset"]
    if lv.enabled():
        mutations.append("closed_phase")
    mutation = choice(mutations)
    if mutation == "fuzz":
        built = _vault_deposit_base(accounts, vaults)
        if built is None:
            return
        base, wallet = built
        await submit_fuzzed("VaultDeposit", base, client, wallet)
        return
    if mutation == "fake_vault":
        if not vaults:
            return
        vault = choice(vaults)
        txn = VaultDeposit(
            account=depositor.address,
            vault_id=params.fake_id(),
            amount=_amount_for_asset(vault.asset),
        )
    elif mutation == "zero_amount":
        if not vaults:
            return
        vault = choice(vaults)
        txn = VaultDeposit(
            account=depositor.address,
            vault_id=vault.vault_id,
            amount="0",
        )
    elif mutation == "mismatched_asset":
        if not vaults:
            return
        vault = choice(vaults)
        if isinstance(vault.asset, xrpl.models.XRP):
            amount = IOUAmount(currency="USD", issuer=depositor.address, value=params.iou_amount())
        else:
            amount = params.vault_deposit_amount()
        txn = VaultDeposit(
            account=depositor.address,
            vault_id=vault.vault_id,
            amount=amount,
        )
    else:  # closed_phase — subscription window shut, so tecEXPIRED
        closed = _pick_blocked_vault(vaults, _DEPOSIT_BLOCKED)
        if closed is None:
            return
        txn = VaultDeposit(
            account=depositor.address,
            vault_id=closed.vault_id,
            amount=_amount_for_asset(closed.asset),
        )
    await submit_tx("VaultDeposit", txn, client, depositor.wallet)


# ── Withdraw ─────────────────────────────────────────────────────────


async def vault_withdraw(
    accounts: dict[str, UserAccount], vaults: list[Vault], client: AsyncJsonRpcClient
) -> None:
    if params.should_send_faulty():
        return await _vault_withdraw_faulty(accounts, vaults, client)
    return await _vault_withdraw_valid(accounts, vaults, client)


def _state_aware_withdraw_amount(vault: Vault) -> IOUAmount | MPTAmount | str:
    if vault.balance <= 0:
        return _amount_for_asset(vault.asset)
    strategy = choice(["exact", "half", "small"])
    if strategy == "exact":
        amount = vault.balance
    elif strategy == "half":
        amount = max(1, vault.balance // 2)
    else:
        amount = max(1, vault.balance // 4)
    return str(amount)


def _vault_withdraw_base(
    accounts: dict[str, UserAccount], vaults: list[Vault]
) -> tuple[VaultWithdraw, Wallet] | None:
    if not vaults:
        return None
    vault = _pick_permitted_vault(vaults, _WITHDRAW_BLOCKED)
    if vault is None or vault.owner not in accounts:
        return None
    owner = accounts[vault.owner]
    txn = VaultWithdraw(
        account=owner.address,
        vault_id=vault.vault_id,
        amount=_state_aware_withdraw_amount(vault),
    )
    return txn, owner.wallet


async def _vault_withdraw_valid(
    accounts: dict[str, UserAccount], vaults: list[Vault], client: AsyncJsonRpcClient
) -> None:
    built = _vault_withdraw_base(accounts, vaults)
    if built is None:
        return
    txn, wallet = built
    await submit_tx("VaultWithdraw", txn, client, wallet)


async def _vault_withdraw_faulty(
    accounts: dict[str, UserAccount], vaults: list[Vault], client: AsyncJsonRpcClient
) -> None:
    if not accounts or not vaults:
        return
    vault = choice(vaults)
    mutations = ["fuzz", "fake_vault", "non_owner", "overdraw"]
    if lv.enabled():
        mutations.append("closed_phase")
    mutation = choice(mutations)
    if mutation == "closed_phase":
        # Investment phase locks the vault, so a withdraw draws tecTOO_SOON.
        locked = _pick_blocked_vault(vaults, _WITHDRAW_BLOCKED)
        if locked is None or locked.owner not in accounts:
            return
        owner = accounts[locked.owner]
        txn = VaultWithdraw(
            account=owner.address,
            vault_id=locked.vault_id,
            amount=_amount_for_asset(locked.asset),
        )
        await submit_tx("VaultWithdraw", txn, client, owner.wallet)
        return
    if mutation == "fuzz":
        built = _vault_withdraw_base(accounts, vaults)
        if built is None:
            return
        base, wallet = built
        await submit_fuzzed("VaultWithdraw", base, client, wallet)
        return
    if mutation == "overdraw":
        if vault.owner not in accounts:
            return
        owner = accounts[vault.owner]
        if vault.balance > 0:
            amount = str(vault.balance + randint(1, 1_000_000))
        else:
            amount = _amount_for_asset(vault.asset)
        txn = VaultWithdraw(
            account=owner.address,
            vault_id=vault.vault_id,
            amount=amount,
        )
        await submit_tx("VaultWithdraw", txn, client, owner.wallet)
        return
    if mutation == "fake_vault":
        if vault.owner not in accounts:
            return
        owner = accounts[vault.owner]
        txn = VaultWithdraw(
            account=owner.address,
            vault_id=params.fake_id(),
            amount=_amount_for_asset(vault.asset),
        )
        await submit_tx("VaultWithdraw", txn, client, owner.wallet)
    else:  # non_owner
        non_owners = [a for a in accounts.values() if a.address != vault.owner]
        if not non_owners:
            return
        impostor = choice(non_owners)
        txn = VaultWithdraw(
            account=impostor.address,
            vault_id=vault.vault_id,
            amount=_amount_for_asset(vault.asset),
        )
        await submit_tx("VaultWithdraw", txn, client, impostor.wallet)


# ── Set ──────────────────────────────────────────────────────────────


async def vault_set(
    accounts: dict[str, UserAccount], vaults: list[Vault], client: AsyncJsonRpcClient
) -> None:
    if params.should_send_faulty():
        return await _vault_set_faulty(accounts, vaults, client)
    return await _vault_set_valid(accounts, vaults, client)


def _vault_set_base(
    accounts: dict[str, UserAccount], vaults: list[Vault]
) -> tuple[VaultSet, Wallet] | None:
    if not vaults:
        return None
    vault = choice(vaults)
    if vault.owner not in accounts:
        return None
    owner = accounts[vault.owner]
    txn = VaultSet(
        account=owner.address,
        vault_id=vault.vault_id,
        assets_maximum=params.vault_assets_maximum(),
        data=params.vault_data(),
    )
    return txn, owner.wallet


async def _vault_set_valid(
    accounts: dict[str, UserAccount], vaults: list[Vault], client: AsyncJsonRpcClient
) -> None:
    built = _vault_set_base(accounts, vaults)
    if built is None:
        return
    txn, wallet = built
    await submit_tx("VaultSet", txn, client, wallet)


async def _vault_set_faulty(
    accounts: dict[str, UserAccount], vaults: list[Vault], client: AsyncJsonRpcClient
) -> None:
    if not accounts or not vaults:
        return
    vault = choice(vaults)
    mutation = choice(["fuzz", "fake_vault", "non_owner"])
    if mutation == "fuzz":
        built = _vault_set_base(accounts, vaults)
        if built is None:
            return
        base, wallet = built
        await submit_fuzzed("VaultSet", base, client, wallet)
        return
    if mutation == "fake_vault":
        if vault.owner not in accounts:
            return
        owner = accounts[vault.owner]
        txn = VaultSet(
            account=owner.address,
            vault_id=params.fake_id(),
            assets_maximum=params.vault_assets_maximum(),
            data=params.vault_data(),
        )
        await submit_tx("VaultSet", txn, client, owner.wallet)
    else:  # non_owner
        non_owners = [a for a in accounts.values() if a.address != vault.owner]
        if not non_owners:
            return
        impostor = choice(non_owners)
        txn = VaultSet(
            account=impostor.address,
            vault_id=vault.vault_id,
            assets_maximum=params.vault_assets_maximum(),
            data=params.vault_data(),
        )
        await submit_tx("VaultSet", txn, client, impostor.wallet)


# ── Delete ───────────────────────────────────────────────────────────


async def vault_delete(
    accounts: dict[str, UserAccount], vaults: list[Vault], client: AsyncJsonRpcClient
) -> None:
    if params.should_send_faulty():
        return await _vault_delete_faulty(accounts, vaults, client)
    return await _vault_delete_valid(accounts, vaults, client)


def _vault_delete_base(
    accounts: dict[str, UserAccount], vaults: list[Vault]
) -> tuple[VaultDelete, Wallet] | None:
    if not vaults:
        return None
    # Non-empty vaults return tecNO_PERMISSION
    empty = [v for v in vaults if v.balance <= 0 and v.owner in accounts]
    vault = choice(empty) if empty else choice(vaults)
    if vault.owner not in accounts:
        return None
    owner = accounts[vault.owner]
    # MemoData is temDISABLED without the amendment, so it only joins once a
    # validated Vault proved it active.
    memo = (
        params.vault_delete_memo_data()
        if lv.enabled() and params.should_attach_delete_memo()
        else None
    )
    txn = VaultDelete(
        account=owner.address,
        vault_id=vault.vault_id,
        memo_data=memo,
    )
    return txn, owner.wallet


async def _vault_delete_valid(
    accounts: dict[str, UserAccount], vaults: list[Vault], client: AsyncJsonRpcClient
) -> None:
    built = _vault_delete_base(accounts, vaults)
    if built is None:
        return
    txn, wallet = built
    await submit_tx("VaultDelete", txn, client, wallet)


async def _vault_delete_faulty(
    accounts: dict[str, UserAccount], vaults: list[Vault], client: AsyncJsonRpcClient
) -> None:
    if not accounts or not vaults:
        return
    vault = choice(vaults)
    mutations = ["fuzz", "fake_vault", "non_owner"]
    if lv.enabled():
        mutations += ["empty_memo", "oversized_memo"]
    mutation = choice(mutations)
    if mutation == "fuzz":
        built = _vault_delete_base(accounts, vaults)
        if built is None:
            return
        base, wallet = built
        await submit_fuzzed("VaultDelete", base, client, wallet)
        return
    if mutation in ("empty_memo", "oversized_memo"):
        # validDataLength rejects both an empty and an over-256-byte MemoData.
        if vault.owner not in accounts:
            return
        owner = accounts[vault.owner]
        txn = VaultDelete(
            account=owner.address,
            vault_id=vault.vault_id,
            memo_data=(
                "" if mutation == "empty_memo" else params.oversized_vault_delete_memo_data()
            ),
        )
        await submit_tx("VaultDelete", txn, client, owner.wallet)
        return
    if mutation == "fake_vault":
        if vault.owner not in accounts:
            return
        owner = accounts[vault.owner]
        txn = VaultDelete(
            account=owner.address,
            vault_id=params.fake_id(),
        )
        await submit_tx("VaultDelete", txn, client, owner.wallet)
    else:  # non_owner
        non_owners = [a for a in accounts.values() if a.address != vault.owner]
        if not non_owners:
            return
        impostor = choice(non_owners)
        txn = VaultDelete(
            account=impostor.address,
            vault_id=vault.vault_id,
        )
        await submit_tx("VaultDelete", txn, client, impostor.wallet)


# ── Clawback ─────────────────────────────────────────────────────────


async def vault_clawback(
    accounts: dict[str, UserAccount], vaults: list[Vault], client: AsyncJsonRpcClient
) -> None:
    if params.should_send_faulty():
        return await _vault_clawback_faulty(accounts, vaults, client)
    return await _vault_clawback_valid(accounts, vaults, client)


def _get_asset_issuer(vault: Vault) -> str | None:
    """Issuer address for the vault's asset, or None for XRP."""
    if isinstance(vault.asset, IssuedCurrency):
        issuer: str = vault.asset.issuer
        return issuer
    if isinstance(vault.asset, MPTCurrency):
        # MPT issuer address not reachable from the issuance ID here; skip.
        return None
    return None


def _vault_clawback_base(
    accounts: dict[str, UserAccount], vaults: list[Vault]
) -> tuple[VaultClawback, Wallet] | None:
    if not vaults:
        return None
    # VaultClawback must be submitted by the asset issuer, not the vault owner.
    eligible = [v for v in vaults if _get_asset_issuer(v) in accounts and v.shareholders]
    if not eligible:
        return None
    vault = choice(eligible)
    issuer_address = _get_asset_issuer(vault)
    if issuer_address is None:
        return None
    issuer = accounts[issuer_address]
    amount = _clawback_amount(vault.asset)
    if amount is None:
        return None
    holder = choice(list(vault.shareholders))
    txn = VaultClawback(
        account=issuer.address,
        vault_id=vault.vault_id,
        holder=holder,
        amount=amount,
    )
    return txn, issuer.wallet


async def _vault_clawback_valid(
    accounts: dict[str, UserAccount], vaults: list[Vault], client: AsyncJsonRpcClient
) -> None:
    built = _vault_clawback_base(accounts, vaults)
    if built is None:
        return
    txn, wallet = built
    await submit_tx("VaultClawback", txn, client, wallet)


async def _vault_clawback_faulty(
    accounts: dict[str, UserAccount], vaults: list[Vault], client: AsyncJsonRpcClient
) -> None:
    if not accounts or not vaults:
        return
    vault = choice(vaults)
    if vault.owner not in accounts:
        return
    owner = accounts[vault.owner]
    amount = _clawback_amount(vault.asset)
    if amount is None:
        return
    mutation = choice(["fuzz", "fake_vault", "clawback_self"])
    if mutation == "fuzz":
        built = _vault_clawback_base(accounts, vaults)
        if built is None:
            return
        base, wallet = built
        await submit_fuzzed("VaultClawback", base, client, wallet)
        return
    if mutation == "fake_vault":
        other_accounts = [a for a in accounts if a != vault.owner]
        if not other_accounts:
            return
        holder = choice(other_accounts)
        txn = VaultClawback(
            account=owner.address,
            vault_id=params.fake_id(),
            holder=holder,
            amount=amount,
        )
    else:  # clawback_self
        txn = VaultClawback(
            account=owner.address,
            vault_id=vault.vault_id,
            holder=owner.address,
            amount=amount,
        )
    await submit_tx("VaultClawback", txn, client, owner.wallet)

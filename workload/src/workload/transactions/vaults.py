"""Vault transaction generators for the antithesis workload."""

import xrpl.models
from xrpl.asyncio.clients import AsyncJsonRpcClient
from xrpl.models import IssuedCurrency
from xrpl.models import IssuedCurrencyAmount as IOUAmount
from xrpl.models.amounts import MPTAmount
from xrpl.models.currencies import MPTCurrency
from xrpl.models.transactions import (
    VaultClawback,
    VaultCreate,
    VaultDelete,
    VaultDeposit,
    VaultSet,
    VaultWithdraw,
)

from workload import params
from workload.models import MPTokenIssuance, TrustLine, UserAccount, Vault
from workload.randoms import choice, randint, random
from workload.submit import submit_tx

# ── Create ───────────────────────────────────────────────────────────


def _amount_for_asset(asset: object) -> IOUAmount | MPTAmount | str:
    """Create an Amount matching the vault's asset type."""
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
    # XRP — return drops as string
    return params.vault_deposit_amount()


def _random_asset(
    trust_lines: list[TrustLine], mpt_issuances: list[MPTokenIssuance]
) -> IssuedCurrency | MPTCurrency | xrpl.models.XRP:
    """Pick a random asset: XRP, IOU, or MPT based on available state."""
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


async def _vault_create_valid(
    accounts: dict[str, UserAccount],
    vaults: list[Vault],
    trust_lines: list[TrustLine],
    mpt_issuances: list[MPTokenIssuance],
    client: AsyncJsonRpcClient,
) -> None:
    src_address = choice(list(accounts))
    src = accounts[src_address]
    asset = _random_asset(trust_lines, mpt_issuances)
    txn = VaultCreate(
        account=src.address,
        asset=asset,
        assets_maximum=params.vault_assets_maximum(),
        data=params.vault_data(),
    )
    await submit_tx("VaultCreate", txn, client, src.wallet)


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
    mutation = choice(["zero_max", "oversized_data", "xrp_with_issuer"])
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
    else:  # xrp_with_issuer
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
    await submit_tx("VaultCreate", txn, client, src.wallet)


# ── Deposit ──────────────────────────────────────────────────────────


async def vault_deposit(
    accounts: dict[str, UserAccount], vaults: list[Vault], client: AsyncJsonRpcClient
) -> None:
    if params.should_send_faulty():
        return await _vault_deposit_faulty(accounts, vaults, client)
    return await _vault_deposit_valid(accounts, vaults, client)


async def _vault_deposit_valid(
    accounts: dict[str, UserAccount], vaults: list[Vault], client: AsyncJsonRpcClient
) -> None:
    if not vaults:
        return
    vault = choice(vaults)
    depositor_id = choice(list(accounts))
    depositor = accounts[depositor_id]
    txn = VaultDeposit(
        account=depositor.address,
        vault_id=vault.vault_id,
        amount=_amount_for_asset(vault.asset),
    )
    await submit_tx("VaultDeposit", txn, client, depositor.wallet)


async def _vault_deposit_faulty(
    accounts: dict[str, UserAccount], vaults: list[Vault], client: AsyncJsonRpcClient
) -> None:
    if not accounts:
        return
    depositor = choice(list(accounts.values()))
    mutation = choice(["fake_vault", "zero_amount", "mismatched_asset"])
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
    else:  # mismatched_asset
        if not vaults:
            return
        vault = choice(vaults)
        if isinstance(vault.asset, xrpl.models.XRP):
            # Vault is XRP, deposit IOU instead
            amount = IOUAmount(currency="USD", issuer=depositor.address, value=params.iou_amount())
        else:
            # Vault is IOU/MPT, deposit XRP drops instead
            amount = params.vault_deposit_amount()
        txn = VaultDeposit(
            account=depositor.address,
            vault_id=vault.vault_id,
            amount=amount,
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
    """Generate a withdraw amount informed by tracked vault balance."""
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


async def _vault_withdraw_valid(
    accounts: dict[str, UserAccount], vaults: list[Vault], client: AsyncJsonRpcClient
) -> None:
    if not vaults:
        return
    vault = choice(vaults)
    if vault.owner not in accounts:
        return
    owner = accounts[vault.owner]
    txn = VaultWithdraw(
        account=owner.address,
        vault_id=vault.vault_id,
        amount=_state_aware_withdraw_amount(vault),
    )
    await submit_tx("VaultWithdraw", txn, client, owner.wallet)


async def _vault_withdraw_faulty(
    accounts: dict[str, UserAccount], vaults: list[Vault], client: AsyncJsonRpcClient
) -> None:
    if not accounts or not vaults:
        return
    vault = choice(vaults)
    mutation = choice(["fake_vault", "non_owner", "overdraw"])
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


async def _vault_set_valid(
    accounts: dict[str, UserAccount], vaults: list[Vault], client: AsyncJsonRpcClient
) -> None:
    if not vaults:
        return
    vault = choice(vaults)
    if vault.owner not in accounts:
        return
    owner = accounts[vault.owner]
    txn = VaultSet(
        account=owner.address,
        vault_id=vault.vault_id,
        assets_maximum=params.vault_assets_maximum(),
        data=params.vault_data(),
    )
    await submit_tx("VaultSet", txn, client, owner.wallet)


async def _vault_set_faulty(
    accounts: dict[str, UserAccount], vaults: list[Vault], client: AsyncJsonRpcClient
) -> None:
    if not accounts or not vaults:
        return
    vault = choice(vaults)
    mutation = choice(["fake_vault", "non_owner"])
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


async def _vault_delete_valid(
    accounts: dict[str, UserAccount], vaults: list[Vault], client: AsyncJsonRpcClient
) -> None:
    if not vaults:
        return
    # Prefer vaults with zero balance — non-empty vaults return tecNO_PERMISSION
    empty = [v for v in vaults if v.balance <= 0 and v.owner in accounts]
    vault = choice(empty) if empty else choice(vaults)
    if vault.owner not in accounts:
        return
    owner = accounts[vault.owner]
    txn = VaultDelete(
        account=owner.address,
        vault_id=vault.vault_id,
    )
    await submit_tx("VaultDelete", txn, client, owner.wallet)


async def _vault_delete_faulty(
    accounts: dict[str, UserAccount], vaults: list[Vault], client: AsyncJsonRpcClient
) -> None:
    if not accounts or not vaults:
        return
    vault = choice(vaults)
    mutation = choice(["fake_vault", "non_owner"])
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
    """Return the issuer address for the vault's asset, or None for XRP."""
    if isinstance(vault.asset, IssuedCurrency):
        return vault.asset.issuer
    if isinstance(vault.asset, MPTCurrency):
        # MPT issuance ID encodes the issuer — but we need the address.
        # The issuer is stored in the MPTokenIssuance model, but we don't have
        # that here. For now, return None and let the caller skip.
        return None
    return None


async def _vault_clawback_valid(
    accounts: dict[str, UserAccount], vaults: list[Vault], client: AsyncJsonRpcClient
) -> None:
    if not vaults:
        return
    # VaultClawback must be submitted by the asset issuer, not the vault owner.
    # Filter to IOU vaults where the issuer is known and shareholders exist.
    eligible = [v for v in vaults if _get_asset_issuer(v) in accounts and v.shareholders]
    if not eligible:
        return
    vault = choice(eligible)
    issuer_address = _get_asset_issuer(vault)
    issuer = accounts[issuer_address]
    # Pick a known shareholder (someone who actually deposited)
    holder = choice(list(vault.shareholders))
    txn = VaultClawback(
        account=issuer.address,
        vault_id=vault.vault_id,
        holder=holder,
        amount=_amount_for_asset(vault.asset),
    )
    await submit_tx("VaultClawback", txn, client, issuer.wallet)


async def _vault_clawback_faulty(
    accounts: dict[str, UserAccount], vaults: list[Vault], client: AsyncJsonRpcClient
) -> None:
    if not accounts or not vaults:
        return
    vault = choice(vaults)
    if vault.owner not in accounts:
        return
    owner = accounts[vault.owner]
    mutation = choice(["fake_vault", "clawback_self"])
    if mutation == "fake_vault":
        other_accounts = [a for a in accounts if a != vault.owner]
        if not other_accounts:
            return
        holder = choice(other_accounts)
        txn = VaultClawback(
            account=owner.address,
            vault_id=params.fake_id(),
            holder=holder,
            amount=_amount_for_asset(vault.asset),
        )
    else:  # clawback_self — owner == holder
        txn = VaultClawback(
            account=owner.address,
            vault_id=vault.vault_id,
            holder=owner.address,
            amount=_amount_for_asset(vault.asset),
        )
    await submit_tx("VaultClawback", txn, client, owner.wallet)

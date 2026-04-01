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

from workload import logging, params
from workload.models import MPTokenIssuance, TrustLine, UserAccount, Vault
from workload.randoms import choice, random
from workload.submit import submit_tx

log = logging.getLogger(__name__)


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
    pass  # TODO: fault injection


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
        log.debug("No vaults to deposit into")
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
    pass  # TODO: fault injection


# ── Withdraw ─────────────────────────────────────────────────────────


async def vault_withdraw(
    accounts: dict[str, UserAccount], vaults: list[Vault], client: AsyncJsonRpcClient
) -> None:
    if params.should_send_faulty():
        return await _vault_withdraw_faulty(accounts, vaults, client)
    return await _vault_withdraw_valid(accounts, vaults, client)


async def _vault_withdraw_valid(
    accounts: dict[str, UserAccount], vaults: list[Vault], client: AsyncJsonRpcClient
) -> None:
    if not vaults:
        log.debug("No vaults to withdraw from")
        return
    vault = choice(vaults)
    if vault.owner not in accounts:
        return
    owner = accounts[vault.owner]
    txn = VaultWithdraw(
        account=owner.address,
        vault_id=vault.vault_id,
        amount=_amount_for_asset(vault.asset),
    )
    await submit_tx("VaultWithdraw", txn, client, owner.wallet)


async def _vault_withdraw_faulty(
    accounts: dict[str, UserAccount], vaults: list[Vault], client: AsyncJsonRpcClient
) -> None:
    pass  # TODO: fault injection


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
        log.debug("No vaults to modify")
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
    pass  # TODO: fault injection


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
        log.debug("No vaults to delete")
        return
    vault = choice(vaults)
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
    pass  # TODO: fault injection


# ── Clawback ─────────────────────────────────────────────────────────


async def vault_clawback(
    accounts: dict[str, UserAccount], vaults: list[Vault], client: AsyncJsonRpcClient
) -> None:
    if params.should_send_faulty():
        return await _vault_clawback_faulty(accounts, vaults, client)
    return await _vault_clawback_valid(accounts, vaults, client)


async def _vault_clawback_valid(
    accounts: dict[str, UserAccount], vaults: list[Vault], client: AsyncJsonRpcClient
) -> None:
    if not vaults:
        log.debug("No vaults for clawback")
        return
    vault = choice(vaults)
    if vault.owner not in accounts:
        return
    owner = accounts[vault.owner]
    other_accounts = [a for a in accounts if a != vault.owner]
    if not other_accounts:
        return
    holder = choice(other_accounts)
    txn = VaultClawback(
        account=owner.address,
        vault_id=vault.vault_id,
        holder=holder,
    )
    await submit_tx("VaultClawback", txn, client, owner.wallet)


async def _vault_clawback_faulty(
    accounts: dict[str, UserAccount], vaults: list[Vault], client: AsyncJsonRpcClient
) -> None:
    pass  # TODO: fault injection

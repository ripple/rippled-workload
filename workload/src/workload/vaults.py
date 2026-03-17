"""Vault transaction generators for the antithesis workload."""

import xrpl.models
from workload import logging, params
from workload.assertions import tx_submitted, tx_result
from workload.models import Vault
from workload.randoms import choice, random
from xrpl.asyncio.transaction import submit_and_wait
from xrpl.models import IssuedCurrency, IssuedCurrencyAmount as IOUAmount
from xrpl.models.amounts import MPTAmount
from xrpl.models.currencies import MPTCurrency
from xrpl.models.transactions import (
    VaultCreate,
    VaultSet,
    VaultDelete,
    VaultDeposit,
    VaultWithdraw,
    VaultClawback,
)

log = logging.getLogger(__name__)


def _extract_created_id(result, ledger_entry_type):
    """Extract the LedgerIndex of a newly created object from tx metadata."""
    for node in result.get("meta", {}).get("AffectedNodes", []):
        created = node.get("CreatedNode", {})
        if created.get("LedgerEntryType") == ledger_entry_type:
            return created.get("LedgerIndex", "")
    return None


# ── Create ───────────────────────────────────────────────────────────

def _amount_for_asset(asset):
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


def _random_asset(trust_lines, mpt_issuances):
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


async def vault_create(accounts, vaults, trust_lines, mpt_issuances, client):
    if not accounts:
        return
    if params.should_send_faulty():
        return await _vault_create_faulty(accounts, vaults, trust_lines, mpt_issuances, client)
    return await _vault_create_valid(accounts, vaults, trust_lines, mpt_issuances, client)


async def _vault_create_valid(accounts, vaults, trust_lines, mpt_issuances, client):
    src_address = choice(list(accounts))
    src = accounts[src_address]
    asset = _random_asset(trust_lines, mpt_issuances)
    txn = VaultCreate(
        account=src.address,
        asset=asset,
        assets_maximum=params.vault_assets_maximum(),
        data=params.vault_data(),
    )
    tx_submitted("VaultCreate")
    response = await submit_and_wait(txn, client, src.wallet)
    result = response.result
    tx_result("VaultCreate", result)
    if result.get("engine_result") == "tesSUCCESS":
        vault_id = _extract_created_id(result, "Vault")
        if vault_id:
            vaults.append(Vault(owner=src.address, vault_id=vault_id, asset=asset))
            log.info("Created vault %s with asset %s", vault_id, asset)


async def _vault_create_faulty(accounts, vaults, trust_lines, mpt_issuances, client):
    pass  # TODO: fault injection


# ── Deposit ──────────────────────────────────────────────────────────

async def vault_deposit(accounts, vaults, client):
    if not accounts:
        return
    if params.should_send_faulty():
        return await _vault_deposit_faulty(accounts, vaults, client)
    return await _vault_deposit_valid(accounts, vaults, client)


async def _vault_deposit_valid(accounts, vaults, client):
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
    tx_submitted("VaultDeposit")
    response = await submit_and_wait(txn, client, depositor.wallet)
    tx_result("VaultDeposit", response.result)


async def _vault_deposit_faulty(accounts, vaults, client):
    pass  # TODO: fault injection


# ── Withdraw ─────────────────────────────────────────────────────────

async def vault_withdraw(accounts, vaults, client):
    if not accounts:
        return
    if params.should_send_faulty():
        return await _vault_withdraw_faulty(accounts, vaults, client)
    return await _vault_withdraw_valid(accounts, vaults, client)


async def _vault_withdraw_valid(accounts, vaults, client):
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
    tx_submitted("VaultWithdraw")
    response = await submit_and_wait(txn, client, owner.wallet)
    tx_result("VaultWithdraw", response.result)


async def _vault_withdraw_faulty(accounts, vaults, client):
    pass  # TODO: fault injection


# ── Set ──────────────────────────────────────────────────────────────

async def vault_set(accounts, vaults, client):
    if not accounts:
        return
    if params.should_send_faulty():
        return await _vault_set_faulty(accounts, vaults, client)
    return await _vault_set_valid(accounts, vaults, client)


async def _vault_set_valid(accounts, vaults, client):
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
    tx_submitted("VaultSet")
    response = await submit_and_wait(txn, client, owner.wallet)
    tx_result("VaultSet", response.result)


async def _vault_set_faulty(accounts, vaults, client):
    pass  # TODO: fault injection


# ── Delete ───────────────────────────────────────────────────────────

async def vault_delete(accounts, vaults, client):
    if not accounts:
        return
    if params.should_send_faulty():
        return await _vault_delete_faulty(accounts, vaults, client)
    return await _vault_delete_valid(accounts, vaults, client)


async def _vault_delete_valid(accounts, vaults, client):
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
    tx_submitted("VaultDelete")
    response = await submit_and_wait(txn, client, owner.wallet)
    tx_result("VaultDelete", response.result)
    if response.result.get("engine_result") == "tesSUCCESS":
        vaults.remove(vault)


async def _vault_delete_faulty(accounts, vaults, client):
    pass  # TODO: fault injection


# ── Clawback ─────────────────────────────────────────────────────────

async def vault_clawback(accounts, vaults, client):
    if len(accounts) < 2:
        return
    if params.should_send_faulty():
        return await _vault_clawback_faulty(accounts, vaults, client)
    return await _vault_clawback_valid(accounts, vaults, client)


async def _vault_clawback_valid(accounts, vaults, client):
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
    tx_submitted("VaultClawback")
    response = await submit_and_wait(txn, client, owner.wallet)
    tx_result("VaultClawback", response.result)


async def _vault_clawback_faulty(accounts, vaults, client):
    pass  # TODO: fault injection

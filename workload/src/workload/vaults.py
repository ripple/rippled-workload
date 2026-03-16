"""Vault transaction generators for the antithesis workload."""

import xrpl.models
from workload import logging, params
from workload.assertions import tx_submitted, tx_result
from workload.models import Vault
from workload.randoms import choice
from xrpl.asyncio.transaction import submit_and_wait
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

async def vault_create(accounts, vaults, client):
    if not accounts:
        return
    if params.should_send_faulty():
        return await _vault_create_faulty(accounts, vaults, client)
    return await _vault_create_valid(accounts, vaults, client)


async def _vault_create_valid(accounts, vaults, client):
    src_address = choice(list(accounts))
    src = accounts[src_address]
    txn = VaultCreate(
        account=src.address,
        asset=xrpl.models.XRP(),
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
            vaults.append(Vault(owner=src.address, vault_id=vault_id))
            log.info("Created vault %s", vault_id)


async def _vault_create_faulty(accounts, vaults, client):
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
        amount=params.vault_deposit_amount(),
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
        amount=params.vault_withdraw_amount(),
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

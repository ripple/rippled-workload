"""MPToken transaction generators for the antithesis workload."""

from workload import logging, params
from workload.assertions import tx_submitted, tx_result
from workload.models import MPTokenIssuance
from workload.randoms import choice
from xrpl.asyncio.transaction import submit_and_wait
from xrpl.models.transactions import (
    MPTokenIssuanceCreate,
    MPTokenIssuanceDestroy,
    MPTokenIssuanceSet,
    MPTokenAuthorize,
)

log = logging.getLogger(__name__)


def _extract_created_id(result, ledger_entry_type):
    for node in result.get("meta", {}).get("AffectedNodes", []):
        created = node.get("CreatedNode", {})
        if created.get("LedgerEntryType") == ledger_entry_type:
            return created.get("LedgerIndex", "")
    return None


# ── Create ───────────────────────────────────────────────────────────

async def mpt_create(accounts, mpt_issuances, client):
    if not accounts:
        return
    return await _mpt_create_valid(accounts, mpt_issuances, client)


async def _mpt_create_valid(accounts, mpt_issuances, client):
    src_address = choice(list(accounts))
    src = accounts[src_address]
    txn = MPTokenIssuanceCreate(
        account=src.address,
        maximum_amount=params.mpt_maximum_amount(),
        mptoken_metadata=params.mpt_metadata(),
    )
    tx_submitted("MPTokenIssuanceCreate", txn)
    response = await submit_and_wait(txn, client, src.wallet)
    result = response.result
    tx_result("MPTokenIssuanceCreate", result)
    if result.get("engine_result") == "tesSUCCESS":
        mpt_id = _extract_created_id(result, "MPTokenIssuance")
        if mpt_id:
            mpt_issuances.append(MPTokenIssuance(issuer=src.address, mpt_issuance_id=mpt_id))
            log.info("Created MPT issuance %s", mpt_id)


# ── Authorize ────────────────────────────────────────────────────────

async def mpt_authorize(accounts, mpt_issuances, client):
    if len(accounts) < 2:
        return
    if params.should_send_faulty():
        return await _mpt_authorize_faulty(accounts, mpt_issuances, client)
    return await _mpt_authorize_valid(accounts, mpt_issuances, client)


async def _mpt_authorize_valid(accounts, mpt_issuances, client):
    if not mpt_issuances:
        log.debug("No MPT issuances to authorize")
        return
    mpt = choice(mpt_issuances)
    if mpt.issuer not in accounts:
        return
    issuer = accounts[mpt.issuer]
    other_accounts = [a for a in accounts if a != mpt.issuer]
    if not other_accounts:
        return
    holder_id = choice(other_accounts)
    txn = MPTokenAuthorize(
        account=issuer.address,
        mptoken_issuance_id=mpt.mpt_issuance_id,
        holder=holder_id,
    )
    tx_submitted("MPTokenAuthorize", txn)
    response = await submit_and_wait(txn, client, issuer.wallet)
    tx_result("MPTokenAuthorize", response.result)


async def _mpt_authorize_faulty(accounts, mpt_issuances, client):
    pass  # TODO: fault injection


# ── Set ──────────────────────────────────────────────────────────────

async def mpt_issuance_set(accounts, mpt_issuances, client):
    if not accounts:
        return
    if params.should_send_faulty():
        return await _mpt_issuance_set_faulty(accounts, mpt_issuances, client)
    return await _mpt_issuance_set_valid(accounts, mpt_issuances, client)


async def _mpt_issuance_set_valid(accounts, mpt_issuances, client):
    if not mpt_issuances:
        log.debug("No MPT issuances to set")
        return
    mpt = choice(mpt_issuances)
    if mpt.issuer not in accounts:
        return
    issuer = accounts[mpt.issuer]
    txn = MPTokenIssuanceSet(
        account=issuer.address,
        mptoken_issuance_id=mpt.mpt_issuance_id,
    )
    tx_submitted("MPTokenIssuanceSet", txn)
    response = await submit_and_wait(txn, client, issuer.wallet)
    tx_result("MPTokenIssuanceSet", response.result)


async def _mpt_issuance_set_faulty(accounts, mpt_issuances, client):
    pass  # TODO: fault injection


# ── Destroy ──────────────────────────────────────────────────────────

async def mpt_destroy(accounts, mpt_issuances, client):
    if not accounts:
        return
    if params.should_send_faulty():
        return await _mpt_destroy_faulty(accounts, mpt_issuances, client)
    return await _mpt_destroy_valid(accounts, mpt_issuances, client)


async def _mpt_destroy_valid(accounts, mpt_issuances, client):
    if not mpt_issuances:
        log.debug("No MPT issuances to destroy")
        return
    mpt = choice(mpt_issuances)
    if mpt.issuer not in accounts:
        return
    issuer = accounts[mpt.issuer]
    txn = MPTokenIssuanceDestroy(
        account=issuer.address,
        mptoken_issuance_id=mpt.mpt_issuance_id,
    )
    tx_submitted("MPTokenIssuanceDestroy", txn)
    response = await submit_and_wait(txn, client, issuer.wallet)
    tx_result("MPTokenIssuanceDestroy", response.result)
    if response.result.get("engine_result") == "tesSUCCESS":
        mpt_issuances.remove(mpt)


async def _mpt_destroy_faulty(accounts, mpt_issuances, client):
    pass  # TODO: fault injection

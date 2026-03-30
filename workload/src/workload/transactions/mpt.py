"""MPToken transaction generators for the antithesis workload."""

from workload import logging, params
from workload.randoms import choice
from workload.submit import submit_tx
from xrpl.models.transactions import (
    MPTokenIssuanceCreate,
    MPTokenIssuanceDestroy,
    MPTokenIssuanceSet,
    MPTokenAuthorize,
)

log = logging.getLogger(__name__)


# ── Create ───────────────────────────────────────────────────────────

async def mpt_create(accounts, mpt_issuances, client):
    return await _mpt_create_valid(accounts, mpt_issuances, client)


async def _mpt_create_valid(accounts, mpt_issuances, client):
    src_address = choice(list(accounts))
    src = accounts[src_address]
    txn = MPTokenIssuanceCreate(
        account=src.address,
        maximum_amount=params.mpt_maximum_amount(),
        mptoken_metadata=params.mpt_metadata(),
    )
    await submit_tx("MPTokenIssuanceCreate", txn, client, src.wallet)


# ── Authorize ────────────────────────────────────────────────────────

async def mpt_authorize(accounts, mpt_issuances, client):
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
    await submit_tx("MPTokenAuthorize", txn, client, issuer.wallet)


async def _mpt_authorize_faulty(accounts, mpt_issuances, client):
    pass  # TODO: fault injection


# ── Set ──────────────────────────────────────────────────────────────

async def mpt_issuance_set(accounts, mpt_issuances, client):
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
    await submit_tx("MPTokenIssuanceSet", txn, client, issuer.wallet)


async def _mpt_issuance_set_faulty(accounts, mpt_issuances, client):
    pass  # TODO: fault injection


# ── Destroy ──────────────────────────────────────────────────────────

async def mpt_destroy(accounts, mpt_issuances, client):
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
    await submit_tx("MPTokenIssuanceDestroy", txn, client, issuer.wallet)


async def _mpt_destroy_faulty(accounts, mpt_issuances, client):
    pass  # TODO: fault injection

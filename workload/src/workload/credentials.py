"""Credential transaction generators for the antithesis workload."""

import time

from workload import logging, params
from workload.assertions import tx_submitted, tx_result
from workload.models import Credential
from workload.randoms import sample, choice
from xrpl.asyncio.transaction import submit_and_wait
from xrpl.models.transactions import (
    CredentialCreate,
    CredentialAccept,
    CredentialDelete,
)

log = logging.getLogger(__name__)


# ── Create ───────────────────────────────────────────────────────────

async def credential_create(accounts, credentials, client):
    if len(accounts) < 2:
        return
    if params.should_send_faulty():
        return await _credential_create_faulty(accounts, credentials, client)
    return await _credential_create_valid(accounts, credentials, client)


async def _credential_create_valid(accounts, credentials, client):
    issuer_id, subject_id = sample(list(accounts), 2)
    issuer = accounts[issuer_id]
    cred_type = params.credential_type()
    txn = CredentialCreate(
        account=issuer.address,
        subject=subject_id,
        credential_type=cred_type,
        expiration=int(time.time()) + params.credential_expiration_offset(),
        uri=params.credential_uri(),
    )
    tx_submitted("CredentialCreate")
    response = await submit_and_wait(txn, client, issuer.wallet)
    result = response.result
    tx_result("CredentialCreate", result)
    if result.get("engine_result") == "tesSUCCESS":
        credentials.append(Credential(
            issuer=issuer.address,
            subject=subject_id,
            credential_type=cred_type,
        ))


async def _credential_create_faulty(accounts, credentials, client):
    pass  # TODO: fault injection


# ── Accept ───────────────────────────────────────────────────────────

async def credential_accept(accounts, credentials, client):
    if len(accounts) < 2:
        return
    if params.should_send_faulty():
        return await _credential_accept_faulty(accounts, credentials, client)
    return await _credential_accept_valid(accounts, credentials, client)


async def _credential_accept_valid(accounts, credentials, client):
    unaccepted = [c for c in credentials if c.subject in accounts]
    if not unaccepted:
        log.debug("No credentials to accept")
        return
    cred = choice(unaccepted)
    subject = accounts[cred.subject]
    txn = CredentialAccept(
        account=subject.address,
        issuer=cred.issuer,
        credential_type=cred.credential_type,
    )
    tx_submitted("CredentialAccept")
    response = await submit_and_wait(txn, client, subject.wallet)
    tx_result("CredentialAccept", response.result)


async def _credential_accept_faulty(accounts, credentials, client):
    pass  # TODO: fault injection


# ── Delete ───────────────────────────────────────────────────────────

async def credential_delete(accounts, credentials, client):
    if not accounts:
        return
    if params.should_send_faulty():
        return await _credential_delete_faulty(accounts, credentials, client)
    return await _credential_delete_valid(accounts, credentials, client)


async def _credential_delete_valid(accounts, credentials, client):
    if not credentials:
        log.debug("No credentials to delete")
        return
    cred = choice(credentials)
    if cred.issuer in accounts:
        account = accounts[cred.issuer]
    elif cred.subject in accounts:
        account = accounts[cred.subject]
    else:
        return
    txn = CredentialDelete(
        account=account.address,
        subject=cred.subject,
        credential_type=cred.credential_type,
    )
    tx_submitted("CredentialDelete")
    response = await submit_and_wait(txn, client, account.wallet)
    tx_result("CredentialDelete", response.result)
    if response.result.get("engine_result") == "tesSUCCESS":
        credentials.remove(cred)


async def _credential_delete_faulty(accounts, credentials, client):
    pass  # TODO: fault injection

"""Credential transaction generators for the antithesis workload."""

import time

from xrpl.asyncio.clients import AsyncJsonRpcClient
from xrpl.models.transactions import (
    CredentialAccept,
    CredentialCreate,
    CredentialDelete,
)

from workload import logging, params
from workload.models import Credential, UserAccount
from workload.randoms import choice, sample
from workload.submit import submit_tx

log = logging.getLogger(__name__)


# ── Create ───────────────────────────────────────────────────────────


async def credential_create(
    accounts: dict[str, UserAccount], credentials: list[Credential], client: AsyncJsonRpcClient
) -> None:
    if params.should_send_faulty():
        return await _credential_create_faulty(accounts, credentials, client)
    return await _credential_create_valid(accounts, credentials, client)


async def _credential_create_valid(
    accounts: dict[str, UserAccount], credentials: list[Credential], client: AsyncJsonRpcClient
) -> None:
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
    await submit_tx("CredentialCreate", txn, client, issuer.wallet)


async def _credential_create_faulty(
    accounts: dict[str, UserAccount], credentials: list[Credential], client: AsyncJsonRpcClient
) -> None:
    pass  # TODO: fault injection


# ── Accept ───────────────────────────────────────────────────────────


async def credential_accept(
    accounts: dict[str, UserAccount], credentials: list[Credential], client: AsyncJsonRpcClient
) -> None:
    if params.should_send_faulty():
        return await _credential_accept_faulty(accounts, credentials, client)
    return await _credential_accept_valid(accounts, credentials, client)


async def _credential_accept_valid(
    accounts: dict[str, UserAccount], credentials: list[Credential], client: AsyncJsonRpcClient
) -> None:
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
    await submit_tx("CredentialAccept", txn, client, subject.wallet)


async def _credential_accept_faulty(
    accounts: dict[str, UserAccount], credentials: list[Credential], client: AsyncJsonRpcClient
) -> None:
    pass  # TODO: fault injection


# ── Delete ───────────────────────────────────────────────────────────


async def credential_delete(
    accounts: dict[str, UserAccount], credentials: list[Credential], client: AsyncJsonRpcClient
) -> None:
    if params.should_send_faulty():
        return await _credential_delete_faulty(accounts, credentials, client)
    return await _credential_delete_valid(accounts, credentials, client)


async def _credential_delete_valid(
    accounts: dict[str, UserAccount], credentials: list[Credential], client: AsyncJsonRpcClient
) -> None:
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
    await submit_tx("CredentialDelete", txn, client, account.wallet)


async def _credential_delete_faulty(
    accounts: dict[str, UserAccount], credentials: list[Credential], client: AsyncJsonRpcClient
) -> None:
    pass  # TODO: fault injection

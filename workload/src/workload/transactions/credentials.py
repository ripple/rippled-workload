"""Credential transaction generators."""

import time

from xrpl.asyncio.clients import AsyncJsonRpcClient
from xrpl.models.transactions import (
    CredentialAccept,
    CredentialCreate,
    CredentialDelete,
)
from xrpl.wallet import Wallet

from workload import params
from workload.fuzz import submit_fuzzed
from workload.models import Credential, UserAccount
from workload.randoms import choice, sample
from workload.submit import submit_tx

# ── Create ───────────────────────────────────────────────────────────


async def credential_create(
    accounts: dict[str, UserAccount], credentials: list[Credential], client: AsyncJsonRpcClient
) -> None:
    if params.should_send_faulty():
        return await _credential_create_faulty(accounts, credentials, client)
    return await _credential_create_valid(accounts, credentials, client)


def _credential_create_base(
    accounts: dict[str, UserAccount],
) -> tuple[CredentialCreate, Wallet] | None:
    """Valid CredentialCreate (issuer credentials a subject) + wallet; shared by valid and fuzz."""
    if len(accounts) < 2:
        return None
    issuer_id, subject_id = sample(list(accounts), 2)
    issuer = accounts[issuer_id]
    txn = CredentialCreate(
        account=issuer.address,
        subject=subject_id,
        credential_type=params.credential_type(),
        expiration=int(time.time()) + params.credential_expiration_offset(),
        uri=params.credential_uri(),
    )
    return txn, issuer.wallet


async def _credential_create_valid(
    accounts: dict[str, UserAccount], credentials: list[Credential], client: AsyncJsonRpcClient
) -> None:
    built = _credential_create_base(accounts)
    if built is None:
        return
    txn, wallet = built
    await submit_tx("CredentialCreate", txn, client, wallet)


async def _credential_create_faulty(
    accounts: dict[str, UserAccount], credentials: list[Credential], client: AsyncJsonRpcClient
) -> None:
    if choice(["fuzz", "duplicate"]) == "fuzz":
        built = _credential_create_base(accounts)
        if built is None:
            return
        base, wallet = built
        await submit_fuzzed("CredentialCreate", base, client, wallet)
        return

    # duplicate: re-create an existing credential -> tecDUPLICATE.
    if not credentials:
        return
    cred = choice(credentials)
    if cred.issuer not in accounts:
        return
    issuer = accounts[cred.issuer]
    txn = CredentialCreate(
        account=issuer.address,
        subject=cred.subject,
        credential_type=cred.credential_type,
    )
    await submit_tx("CredentialCreate", txn, client, issuer.wallet)


# ── Accept ───────────────────────────────────────────────────────────


async def credential_accept(
    accounts: dict[str, UserAccount], credentials: list[Credential], client: AsyncJsonRpcClient
) -> None:
    if params.should_send_faulty():
        return await _credential_accept_faulty(accounts, credentials, client)
    return await _credential_accept_valid(accounts, credentials, client)


def _credential_accept_base(
    accounts: dict[str, UserAccount], credentials: list[Credential]
) -> tuple[CredentialAccept, Wallet] | None:
    """Valid CredentialAccept (subject accepts an issued credential) + wallet."""
    subjects = [c for c in credentials if c.subject in accounts]
    if not subjects:
        return None
    cred = choice(subjects)
    subject = accounts[cred.subject]
    txn = CredentialAccept(
        account=subject.address,
        issuer=cred.issuer,
        credential_type=cred.credential_type,
    )
    return txn, subject.wallet


async def _credential_accept_valid(
    accounts: dict[str, UserAccount], credentials: list[Credential], client: AsyncJsonRpcClient
) -> None:
    built = _credential_accept_base(accounts, credentials)
    if built is None:
        return
    txn, wallet = built
    await submit_tx("CredentialAccept", txn, client, wallet)


async def _credential_accept_faulty(
    accounts: dict[str, UserAccount], credentials: list[Credential], client: AsyncJsonRpcClient
) -> None:
    if not accounts:
        return

    mutation = choice(["fuzz", "no_entry", "fake_issuer", "already_accepted"])
    if mutation == "fuzz":
        built = _credential_accept_base(accounts, credentials)
        if built is None:
            return
        base, wallet = built
        await submit_fuzzed("CredentialAccept", base, client, wallet)
        return

    if mutation == "no_entry":
        # Accept a credential type that was never issued -> tecNO_ENTRY.
        if len(accounts) < 2:
            return
        subject_id, issuer_id = sample(list(accounts), 2)
        subject = accounts[subject_id]
        txn = CredentialAccept(
            account=subject.address,
            issuer=issuer_id,
            credential_type=params.credential_type(),
        )
        await submit_tx("CredentialAccept", txn, client, subject.wallet)
        return

    if mutation == "fake_issuer":
        # Issuer account does not exist -> tecNO_ISSUER.
        subject = choice(list(accounts.values()))
        txn = CredentialAccept(
            account=subject.address,
            issuer=params.fake_account(),
            credential_type=params.credential_type(),
        )
        await submit_tx("CredentialAccept", txn, client, subject.wallet)
        return

    # already_accepted: re-accept an accepted credential -> tecDUPLICATE.
    accepted = [c for c in credentials if c.accepted and c.subject in accounts]
    if not accepted:
        return
    cred = choice(accepted)
    subject = accounts[cred.subject]
    txn = CredentialAccept(
        account=subject.address,
        issuer=cred.issuer,
        credential_type=cred.credential_type,
    )
    await submit_tx("CredentialAccept", txn, client, subject.wallet)


# ── Delete ───────────────────────────────────────────────────────────


async def credential_delete(
    accounts: dict[str, UserAccount], credentials: list[Credential], client: AsyncJsonRpcClient
) -> None:
    if params.should_send_faulty():
        return await _credential_delete_faulty(accounts, credentials, client)
    return await _credential_delete_valid(accounts, credentials, client)


def _credential_delete_base(
    accounts: dict[str, UserAccount], credentials: list[Credential]
) -> tuple[CredentialDelete, Wallet] | None:
    """Valid CredentialDelete (issuer or subject deletes the credential) + wallet."""
    if not credentials:
        return None
    cred = choice(credentials)
    if cred.issuer in accounts:
        account = accounts[cred.issuer]
    elif cred.subject in accounts:
        account = accounts[cred.subject]
    else:
        return None
    txn = CredentialDelete(
        account=account.address,
        subject=cred.subject,
        credential_type=cred.credential_type,
    )
    return txn, account.wallet


async def _credential_delete_valid(
    accounts: dict[str, UserAccount], credentials: list[Credential], client: AsyncJsonRpcClient
) -> None:
    built = _credential_delete_base(accounts, credentials)
    if built is None:
        return
    txn, wallet = built
    await submit_tx("CredentialDelete", txn, client, wallet)


async def _credential_delete_faulty(
    accounts: dict[str, UserAccount], credentials: list[Credential], client: AsyncJsonRpcClient
) -> None:
    if not accounts:
        return

    mutation = choice(["fuzz", "no_entry", "third_party"])
    if mutation == "fuzz":
        built = _credential_delete_base(accounts, credentials)
        if built is None:
            return
        base, wallet = built
        await submit_fuzzed("CredentialDelete", base, client, wallet)
        return

    if mutation == "no_entry":
        # Delete a credential type that does not exist -> tecNO_ENTRY.
        if len(accounts) < 2:
            return
        account_id, subject_id = sample(list(accounts), 2)
        account = accounts[account_id]
        txn = CredentialDelete(
            account=account.address,
            subject=subject_id,
            credential_type=params.credential_type(),
        )
        await submit_tx("CredentialDelete", txn, client, account.wallet)
        return

    # third_party: an account that is neither issuer nor subject deletes a
    # non-expired credential -> tecNO_PERMISSION.
    if not credentials:
        return
    cred = choice(credentials)
    outsiders = [a for a in accounts if a != cred.issuer and a != cred.subject]
    if not outsiders:
        return
    src = accounts[choice(outsiders)]
    txn = CredentialDelete(
        account=src.address,
        subject=cred.subject,
        issuer=cred.issuer,
        credential_type=cred.credential_type,
    )
    await submit_tx("CredentialDelete", txn, client, src.wallet)

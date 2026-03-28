"""Permissioned Domain transaction generators for the antithesis workload."""

from workload import logging, params
from workload.randoms import choice
from workload.submit import submit_tx
from xrpl.models.transactions import (
    PermissionedDomainSet,
    PermissionedDomainDelete,
)
from xrpl.models.transactions.deposit_preauth import Credential as XRPLCredential

log = logging.getLogger(__name__)


# ── Set ──────────────────────────────────────────────────────────────

async def permissioned_domain_set(accounts, domains, client):
    if not accounts:
        return
    if params.should_send_faulty():
        return await _permissioned_domain_set_faulty(accounts, domains, client)
    return await _permissioned_domain_set_valid(accounts, domains, client)


async def _permissioned_domain_set_valid(accounts, domains, client):
    src_address = choice(list(accounts))
    src = accounts[src_address]
    num_creds = params.domain_credential_count()
    accepted = [
        XRPLCredential(
            issuer=choice(list(accounts)),
            credential_type=params.credential_type(),
        )
        for _ in range(num_creds)
    ]
    txn = PermissionedDomainSet(
        account=src.address,
        accepted_credentials=accepted,
    )
    await submit_tx("PermissionedDomainSet", txn, client, src.wallet)


async def _permissioned_domain_set_faulty(accounts, domains, client):
    pass  # TODO: fault injection


# ── Delete ───────────────────────────────────────────────────────────

async def permissioned_domain_delete(accounts, domains, client):
    if not accounts:
        return
    if params.should_send_faulty():
        return await _permissioned_domain_delete_faulty(accounts, domains, client)
    return await _permissioned_domain_delete_valid(accounts, domains, client)


async def _permissioned_domain_delete_valid(accounts, domains, client):
    if not domains:
        log.debug("No domains to delete")
        return
    domain = choice(domains)
    if domain.owner not in accounts:
        return
    owner = accounts[domain.owner]
    txn = PermissionedDomainDelete(
        account=owner.address,
        domain_id=domain.domain_id,
    )
    await submit_tx("PermissionedDomainDelete", txn, client, owner.wallet)


async def _permissioned_domain_delete_faulty(accounts, domains, client):
    pass  # TODO: fault injection

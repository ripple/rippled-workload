"""Permissioned Domain transaction generators for the antithesis workload."""

from workload import logging, params
from workload.assertions import tx_submitted, tx_result
from workload.models import PermissionedDomain
from workload.randoms import choice
from xrpl.asyncio.transaction import submit_and_wait
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
    tx_submitted("PermissionedDomainSet", txn)
    response = await submit_and_wait(txn, client, src.wallet)
    result = response.result
    tx_result("PermissionedDomainSet", result)
    if result.get("engine_result") == "tesSUCCESS":
        for node in result.get("meta", {}).get("AffectedNodes", []):
            created = node.get("CreatedNode", {})
            if created.get("LedgerEntryType") == "PermissionedDomain":
                domain_id = created.get("LedgerIndex", "")
                domains.append(PermissionedDomain(owner=src.address, domain_id=domain_id))
                log.info("Created domain %s", domain_id)
                break


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
    tx_submitted("PermissionedDomainDelete", txn)
    response = await submit_and_wait(txn, client, owner.wallet)
    tx_result("PermissionedDomainDelete", response.result)
    if response.result.get("engine_result") == "tesSUCCESS":
        domains.remove(domain)


async def _permissioned_domain_delete_faulty(accounts, domains, client):
    pass  # TODO: fault injection

"""Permissioned Domain transaction generators for the antithesis workload."""

from xrpl.asyncio.clients import AsyncJsonRpcClient
from xrpl.models.transactions import (
    PermissionedDomainDelete,
    PermissionedDomainSet,
)
from xrpl.models.transactions.deposit_preauth import Credential as XRPLCredential

from workload import params
from workload.models import Credential, PermissionedDomain, UserAccount
from workload.randoms import choice, sample
from workload.submit import submit_tx

# ── Set ──────────────────────────────────────────────────────────────


async def permissioned_domain_set(
    accounts: dict[str, UserAccount],
    domains: list[PermissionedDomain],
    credentials: list[Credential],
    client: AsyncJsonRpcClient,
) -> None:
    if params.should_send_faulty():
        return await _permissioned_domain_set_faulty(accounts, domains, credentials, client)
    return await _permissioned_domain_set_valid(accounts, domains, credentials, client)


def _accepted_from_real_credentials(credentials: list[Credential]) -> list[XRPLCredential] | None:
    """Build accepted_credentials from real (preferably accepted) credentials so
    the domain gains actual members — their subjects — not just the owner.

    Random issuer/type pairs (the fallback) match no real credential, leaving the
    domain owner-only and starving the >=2-member paths (PaymentDomain, ticket
    domain payments)."""
    pool = [c for c in credentials if c.accepted] or credentials
    if not pool:
        return None
    pairs = list({(c.issuer, c.credential_type) for c in pool})
    chosen = sample(pairs, min(len(pairs), params.domain_credential_count()))
    return [XRPLCredential(issuer=issuer, credential_type=ctype) for issuer, ctype in chosen]


def _accepted_random(accounts: dict[str, UserAccount]) -> list[XRPLCredential]:
    return [
        XRPLCredential(issuer=choice(list(accounts)), credential_type=params.credential_type())
        for _ in range(params.domain_credential_count())
    ]


async def _permissioned_domain_set_valid(
    accounts: dict[str, UserAccount],
    domains: list[PermissionedDomain],
    credentials: list[Credential],
    client: AsyncJsonRpcClient,
) -> None:
    src_address = choice(list(accounts))
    src = accounts[src_address]
    accepted = _accepted_from_real_credentials(credentials) or _accepted_random(accounts)
    txn = PermissionedDomainSet(
        account=src.address,
        accepted_credentials=accepted,
    )
    await submit_tx("PermissionedDomainSet", txn, client, src.wallet)


async def _permissioned_domain_set_faulty(
    accounts: dict[str, UserAccount],
    domains: list[PermissionedDomain],
    credentials: list[Credential],
    client: AsyncJsonRpcClient,
) -> None:
    if not accounts:
        return
    src = choice(list(accounts.values()))
    mutation = choice(["fake_issuer", "nonexistent_domain"])
    if mutation == "fake_issuer":
        # Credential issuer that does not exist -> tecNO_ISSUER.
        txn = PermissionedDomainSet(
            account=src.address,
            accepted_credentials=[
                XRPLCredential(
                    issuer=params.fake_account(), credential_type=params.credential_type()
                )
            ],
        )
    else:  # nonexistent_domain — update a domain that does not exist -> tecNO_ENTRY.
        txn = PermissionedDomainSet(
            account=src.address,
            domain_id=params.fake_id(),
            accepted_credentials=[
                XRPLCredential(issuer=src.address, credential_type=params.credential_type())
            ],
        )
    await submit_tx("PermissionedDomainSet", txn, client, src.wallet)


# ── Delete ───────────────────────────────────────────────────────────


async def permissioned_domain_delete(
    accounts: dict[str, UserAccount], domains: list[PermissionedDomain], client: AsyncJsonRpcClient
) -> None:
    if params.should_send_faulty():
        return await _permissioned_domain_delete_faulty(accounts, domains, client)
    return await _permissioned_domain_delete_valid(accounts, domains, client)


async def _permissioned_domain_delete_valid(
    accounts: dict[str, UserAccount], domains: list[PermissionedDomain], client: AsyncJsonRpcClient
) -> None:
    if not domains:
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


async def _permissioned_domain_delete_faulty(
    accounts: dict[str, UserAccount], domains: list[PermissionedDomain], client: AsyncJsonRpcClient
) -> None:
    pass  # TODO: fault injection

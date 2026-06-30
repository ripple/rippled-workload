"""Permissioned Domain transaction generators for the antithesis workload."""

from collections.abc import Callable

from xrpl.asyncio.clients import AsyncJsonRpcClient
from xrpl.models.transactions import (
    PermissionedDomainDelete,
    PermissionedDomainSet,
)
from xrpl.models.transactions.deposit_preauth import Credential as XRPLCredential
from xrpl.wallet import Wallet

from workload import params
from workload.fuzz import submit_fuzzed
from workload.models import Credential, PermissionedDomain, UserAccount
from workload.randoms import choice, sample
from workload.submit import submit_raw, submit_tx

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
    """Use real credentials so the domain gains members; random pairs leave it
    owner-only and starve the >=2-member paths (PaymentDomain, ticket domain payments)."""
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


def _domain_set_base(
    accounts: dict[str, UserAccount],
    domains: list[PermissionedDomain],
    credentials: list[Credential],
) -> tuple[PermissionedDomainSet, Wallet]:
    """Valid PermissionedDomainSet (create/update an owned domain) + wallet."""
    accepted = _accepted_from_real_credentials(credentials) or _accepted_random(accounts)
    own_domains = [d for d in domains if d.owner in accounts]
    if own_domains and params.should_update_domain():
        domain = choice(own_domains)
        src = accounts[domain.owner]
        base = PermissionedDomainSet(
            account=src.address,
            domain_id=domain.domain_id,
            accepted_credentials=accepted,
        )
    else:
        src = accounts[choice(list(accounts))]
        base = PermissionedDomainSet(account=src.address, accepted_credentials=accepted)
    return base, src.wallet


async def _permissioned_domain_set_valid(
    accounts: dict[str, UserAccount],
    domains: list[PermissionedDomain],
    credentials: list[Credential],
    client: AsyncJsonRpcClient,
) -> None:
    base, wallet = _domain_set_base(accounts, domains, credentials)
    await submit_tx("PermissionedDomainSet", base, client, wallet)


async def _permissioned_domain_set_faulty(
    accounts: dict[str, UserAccount],
    domains: list[PermissionedDomain],
    credentials: list[Credential],
    client: AsyncJsonRpcClient,
) -> None:
    if not accounts:
        return
    src = choice(list(accounts.values()))
    mutation = choice(
        [
            "fake_issuer",
            "nonexistent_domain",
            "zero_domain",
            "non_owner_update",
            "empty_credentials",
            "too_many_credentials",
            "duplicate_credentials",
            "fuzz",
        ]
    )

    if mutation == "fuzz":
        base, wallet = _domain_set_base(accounts, domains, credentials)
        await submit_fuzzed("PermissionedDomainSet", base, client, wallet)
        return

    issuer = src.address
    domain_id = None
    mutate: Callable[[dict], None] | None = None

    if mutation == "fake_issuer":
        # Credential issuer that does not exist -> tecNO_ISSUER.
        issuer = params.fake_account()
    elif mutation == "nonexistent_domain":
        # Update a domain that does not exist -> tecNO_ENTRY.
        domain_id = params.fake_id()
    elif mutation == "zero_domain":
        # All-zero DomainID -> temMALFORMED.
        domain_id = params.zero_domain_id()
    elif mutation == "non_owner_update":
        # Update someone else's domain -> tecNO_PERMISSION.
        others = [d for d in domains if d.owner != src.address]
        if not others:
            return
        domain_id = choice(others).domain_id
    else:  # AcceptedCredentials shapes xrpl-py rejects at construction.

        def _cred(ctype: str) -> dict:
            return {"Credential": {"Issuer": src.address, "CredentialType": ctype}}

        def _mutate(d: dict) -> None:
            if mutation == "empty_credentials":  # temARRAY_EMPTY
                d["AcceptedCredentials"] = []
            elif mutation == "too_many_credentials":  # temARRAY_TOO_LARGE (>10 distinct)
                d["AcceptedCredentials"] = [_cred(params.credential_type()) for _ in range(11)]
            else:  # duplicate_credentials -> temMALFORMED (two identical (issuer, type))
                ctype = params.credential_type()
                d["AcceptedCredentials"] = [_cred(ctype), _cred(ctype)]

        mutate = _mutate

    # Base is a valid model; mutate (when set) injects the malformed array post-serialization.
    base = PermissionedDomainSet(
        account=src.address,
        domain_id=domain_id,
        accepted_credentials=[
            XRPLCredential(issuer=issuer, credential_type=params.credential_type())
            for _ in range(2 if mutate else 1)
        ],
    )
    await submit_raw("PermissionedDomainSet", base, client, src.wallet, mutate)


# ── Delete ───────────────────────────────────────────────────────────


async def permissioned_domain_delete(
    accounts: dict[str, UserAccount], domains: list[PermissionedDomain], client: AsyncJsonRpcClient
) -> None:
    if params.should_send_faulty():
        return await _permissioned_domain_delete_faulty(accounts, domains, client)
    return await _permissioned_domain_delete_valid(accounts, domains, client)


def _domain_delete_base(
    accounts: dict[str, UserAccount], domains: list[PermissionedDomain]
) -> tuple[PermissionedDomainDelete, Wallet] | None:
    """Valid PermissionedDomainDelete (owner deletes own domain) + wallet."""
    if not domains:
        return None
    domain = choice(domains)
    if domain.owner not in accounts:
        return None
    owner = accounts[domain.owner]
    base = PermissionedDomainDelete(account=owner.address, domain_id=domain.domain_id)
    return base, owner.wallet


async def _permissioned_domain_delete_valid(
    accounts: dict[str, UserAccount], domains: list[PermissionedDomain], client: AsyncJsonRpcClient
) -> None:
    built = _domain_delete_base(accounts, domains)
    if built is None:
        return
    base, wallet = built
    await submit_tx("PermissionedDomainDelete", base, client, wallet)


async def _permissioned_domain_delete_faulty(
    accounts: dict[str, UserAccount], domains: list[PermissionedDomain], client: AsyncJsonRpcClient
) -> None:
    if not accounts:
        return
    src = choice(list(accounts.values()))
    mutation = choice(["non_owner", "nonexistent", "zero_domain", "fuzz"])
    if mutation == "fuzz":
        built = _domain_delete_base(accounts, domains)
        if built is None:
            return
        base, wallet = built
        await submit_fuzzed("PermissionedDomainDelete", base, client, wallet)
        return
    if mutation == "non_owner":
        # Delete a domain the src does not own -> tecNO_PERMISSION.
        others = [d for d in domains if d.owner != src.address]
        if not others:
            return
        domain_id = choice(others).domain_id
    elif mutation == "nonexistent":
        # Delete a domain that does not exist -> tecNO_ENTRY.
        domain_id = params.fake_id()
    else:  # zero_domain — all-zero DomainID -> temMALFORMED.
        domain_id = params.zero_domain_id()
    base = PermissionedDomainDelete(account=src.address, domain_id=domain_id)
    await submit_raw("PermissionedDomainDelete", base, client, src.wallet)

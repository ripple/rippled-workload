"""Permissioned Domains (XLS-80) workload for rippled.

Exercises the PermissionedDomainSet and PermissionedDomainDelete transactions
as described in XLS-0080 (Permissioned Domains).

This workload requires XLS-70 (Credentials) to be enabled.

Workflow:
    1. domain_set           – Create a new permissioned domain with credential rules
    2. domain_modify        – Modify an existing domain's accepted credentials
    3. domain_delete        – Delete a permissioned domain
    4. domain_set_multiple  – Create a domain with multiple credential types
"""

from __future__ import annotations

import json
from random import SystemRandom
from typing import Any

from xrpl.asyncio.account import get_next_valid_seq_number

from workload import logging
from workload.models import UserAccount
from workload.randoms import sample, choice

urand = SystemRandom()
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_ACCEPTED_CREDENTIALS = 10
MAX_CREDENTIAL_TYPE_LENGTH = 64  # bytes, as per XLS-70d

# ---------------------------------------------------------------------------
# Helper: build the AcceptedCredentials array
# ---------------------------------------------------------------------------

def build_accepted_credentials(credentials: list[dict[str, str]]) -> list[dict]:
    """Build the AcceptedCredentials STArray for a PermissionedDomainSet transaction.

    XLS-80 requires the nested format::

        [{"Credential": {"Issuer": "rABC...", "CredentialType": "123ABC"}}, ...]

    Args:
        credentials: List of dicts with 'issuer' and 'credential_type' keys.

    Returns:
        A list of nested credential dicts ready for the transaction JSON.
    """
    return [
        {
            "Credential": {
                "Issuer": cred["issuer"],
                "CredentialType": cred["credential_type"],
            }
        }
        for cred in credentials
    ]


# ---------------------------------------------------------------------------
# Transaction builders
# ---------------------------------------------------------------------------

def build_permissioned_domain_set_txn(
    account: str,
    accepted_credentials: list[dict[str, str]],
    domain_id: str | None = None,
    sequence: int | None = None,
    fee: str = "12",
) -> dict:
    """Build a PermissionedDomainSet transaction dict.

    Args:
        account:              The account creating/modifying the domain.
        accepted_credentials: List of credential dicts with 'issuer' and 'credential_type'.
        domain_id:            Optional domain ID to modify existing domain.
        sequence:             Optional explicit sequence number.
        fee:                  Transaction fee in drops.

    Returns:
        A raw transaction dict suitable for signing and submission.
    """
    txn: dict[str, Any] = {
        "TransactionType": "PermissionedDomainSet",
        "Account": account,
        "AcceptedCredentials": build_accepted_credentials(accepted_credentials),
        "Fee": fee,
    }
    if domain_id is not None:
        txn["DomainID"] = domain_id
    if sequence is not None:
        txn["Sequence"] = sequence
    return txn


def build_permissioned_domain_delete_txn(
    account: str,
    domain_id: str,
    sequence: int | None = None,
    fee: str = "12",
) -> dict:
    """Build a PermissionedDomainDelete transaction dict.

    Args:
        account:   The domain owner account.
        domain_id: The domain ID to delete.
        sequence:  Optional explicit sequence number.
        fee:       Transaction fee in drops.

    Returns:
        A raw transaction dict suitable for signing and submission.
    """
    txn: dict[str, Any] = {
        "TransactionType": "PermissionedDomainDelete",
        "Account": account,
        "DomainID": domain_id,
        "Fee": fee,
    }
    if sequence is not None:
        txn["Sequence"] = sequence
    return txn


# ---------------------------------------------------------------------------
# High-level async workload functions
# ---------------------------------------------------------------------------

async def domain_set(
    owner: UserAccount,
    credentials: list[dict[str, str]],
    client,
    domain_id: str | None = None,
) -> dict | None:
    """Create or modify a permissioned domain.

    Args:
        owner:       The account creating/modifying the domain.
        credentials: List of credential dicts with 'issuer' and 'credential_type'.
        client:      An AsyncJsonRpcClient instance.
        domain_id:   Optional domain ID to modify existing domain.

    Returns:
        The transaction result dict, or None on failure.
    """
    seq = await get_next_valid_seq_number(owner.address, client)
    txn_dict = build_permissioned_domain_set_txn(
        account=owner.address,
        accepted_credentials=credentials,
        domain_id=domain_id,
        sequence=seq,
    )

    action = "Modifying" if domain_id else "Creating"
    log.info(
        "%s PermissionedDomain: owner=%s, credentials=%s, domain_id=%s",
        action,
        owner.address,
        credentials,
        domain_id,
    )
    log.debug("PermissionedDomainSet txn: %s", json.dumps(txn_dict, indent=2))

    try:
        from xrpl.core.binarycodec import encode_for_signing, encode
        from xrpl.core.keypairs import sign as kp_sign
        import httpx

        txn_dict["SigningPubKey"] = owner.wallet.public_key

        signing_blob = encode_for_signing(txn_dict)
        signature = kp_sign(signing_blob, owner.wallet.private_key)
        txn_dict["TxnSignature"] = signature

        tx_blob = encode(txn_dict)

        payload = {
            "method": "submit",
            "params": [{"tx_blob": tx_blob}],
        }
        async with httpx.AsyncClient() as http_client:
            resp = await http_client.post(str(client.url), json=payload)
            result = resp.json()

        log.info("PermissionedDomainSet result: %s", json.dumps(result, indent=2))
        return result
    except Exception:
        log.exception("PermissionedDomainSet failed for %s", owner)
        return None


async def domain_delete(
    owner: UserAccount,
    domain_id: str,
    client,
) -> dict | None:
    """Delete a permissioned domain.

    Args:
        owner:     The domain owner account.
        domain_id: The domain ID to delete.
        client:    An AsyncJsonRpcClient instance.

    Returns:
        The transaction result dict, or None on failure.
    """
    seq = await get_next_valid_seq_number(owner.address, client)
    txn_dict = build_permissioned_domain_delete_txn(
        account=owner.address,
        domain_id=domain_id,
        sequence=seq,
    )

    log.info(
        "Deleting PermissionedDomain: owner=%s, domain_id=%s",
        owner.address,
        domain_id,
    )
    log.debug("PermissionedDomainDelete txn: %s", json.dumps(txn_dict, indent=2))

    try:
        from xrpl.core.binarycodec import encode_for_signing, encode
        from xrpl.core.keypairs import sign as kp_sign
        import httpx

        txn_dict["SigningPubKey"] = owner.wallet.public_key

        signing_blob = encode_for_signing(txn_dict)
        signature = kp_sign(signing_blob, owner.wallet.private_key)
        txn_dict["TxnSignature"] = signature

        tx_blob = encode(txn_dict)

        payload = {
            "method": "submit",
            "params": [{"tx_blob": tx_blob}],
        }
        async with httpx.AsyncClient() as http_client:
            resp = await http_client.post(str(client.url), json=payload)
            result = resp.json()

        log.info("PermissionedDomainDelete result: %s", json.dumps(result, indent=2))
        return result
    except Exception:
        log.exception("PermissionedDomainDelete failed for %s", owner)
        return None


async def domain_modify(
    owner: UserAccount,
    domain_id: str,
    new_credentials: list[dict[str, str]],
    client,
) -> dict | None:
    """Modify an existing permissioned domain's accepted credentials.

    This is a convenience wrapper around domain_set with a domain_id.

    Args:
        owner:           The domain owner account.
        domain_id:       The domain ID to modify.
        new_credentials: New list of accepted credentials.
        client:          An AsyncJsonRpcClient instance.

    Returns:
        The transaction result dict, or None on failure.
    """
    return await domain_set(owner, new_credentials, client, domain_id=domain_id)


async def domain_set_multiple(
    owner: UserAccount,
    credentials: list[dict[str, str]],
    client,
) -> dict | None:
    """Create a domain with multiple credential types.

    This validates that the credentials list doesn't exceed the maximum.

    Args:
        owner:       The account creating the domain.
        credentials: List of credential dicts (max 10).
        client:      An AsyncJsonRpcClient instance.

    Returns:
        The transaction result dict, or None on failure.

    Raises:
        ValueError: If credentials list is invalid.
    """
    if not credentials:
        raise ValueError("AcceptedCredentials cannot be empty")
    if len(credentials) > MAX_ACCEPTED_CREDENTIALS:
        raise ValueError(
            f"Cannot accept more than {MAX_ACCEPTED_CREDENTIALS} "
            f"credentials (got {len(credentials)})"
        )

    # Validate credential types
    for cred in credentials:
        cred_type = cred.get("credential_type", "")
        if not cred_type:
            raise ValueError("CredentialType cannot be empty")
        # Assuming hex-encoded, so 2 chars per byte
        if len(cred_type) > MAX_CREDENTIAL_TYPE_LENGTH * 2:
            raise ValueError(
                f"CredentialType too long: {len(cred_type)} chars "
                f"(max {MAX_CREDENTIAL_TYPE_LENGTH * 2})"
            )

    log.info(
        "Creating PermissionedDomain with %d credentials: owner=%s",
        len(credentials),
        owner.address,
    )
    return await domain_set(owner, credentials, client)



# ---------------------------------------------------------------------------
# Random / workload-driver helpers
# ---------------------------------------------------------------------------

async def random_domain_set(
    accounts: dict[str, UserAccount],
    client,
) -> dict | None:
    """Create a random permissioned domain.

    Selects a random account as the domain owner and creates a domain
    with 1-5 random credential requirements.

    Args:
        accounts: The workload's account registry {address: UserAccount}.
        client:   An AsyncJsonRpcClient instance.

    Returns:
        The PermissionedDomainSet result, or None on failure.
    """
    if len(accounts) < 2:
        log.warning("Need at least 2 accounts for permissioned domains")
        return None

    # Pick a random owner
    owner_addr = choice(list(accounts))
    owner = accounts[owner_addr]

    # Pick 1-5 random issuers (different from owner)
    other_accounts = [addr for addr in accounts if addr != owner_addr]
    num_credentials = urand.randint(1, min(5, len(other_accounts)))
    issuers = sample(other_accounts, num_credentials)

    # Generate random credential types
    credentials = [
        {
            "issuer": issuer,
            "credential_type": _generate_random_credential_type(),
        }
        for issuer in issuers
    ]

    return await domain_set(owner, credentials, client)


async def random_domain_modify(
    accounts: dict[str, UserAccount],
    domains: list[dict],
    client,
) -> dict | None:
    """Modify a random existing domain.

    Args:
        accounts: The workload's account registry.
        domains:  List of active domain records with 'owner' and 'domain_id'.
        client:   An AsyncJsonRpcClient instance.

    Returns:
        The PermissionedDomainSet result, or None if no domains exist.
    """
    if not domains:
        log.info("No active domains to modify")
        return None

    domain = choice(domains)
    owner = accounts[domain["owner"]]

    # Generate new random credentials
    other_accounts = [addr for addr in accounts if addr != owner.address]
    if not other_accounts:
        log.warning("No other accounts to use as credential issuers")
        return None

    num_credentials = urand.randint(1, min(5, len(other_accounts)))
    issuers = sample(other_accounts, num_credentials)

    new_credentials = [
        {
            "issuer": issuer,
            "credential_type": _generate_random_credential_type(),
        }
        for issuer in issuers
    ]

    return await domain_modify(owner, domain["domain_id"], new_credentials, client)


async def random_domain_delete(
    accounts: dict[str, UserAccount],
    domains: list[dict],
    client,
) -> dict | None:
    """Delete a random existing domain.

    Args:
        accounts: The workload's account registry.
        domains:  List of active domain records with 'owner' and 'domain_id'.
        client:   An AsyncJsonRpcClient instance.

    Returns:
        The PermissionedDomainDelete result, or None if no domains exist.
    """
    if not domains:
        log.info("No active domains to delete")
        return None

    domain = choice(domains)
    owner = accounts[domain["owner"]]

    return await domain_delete(owner, domain["domain_id"], client)


# ---------------------------------------------------------------------------
# Helper utilities
# ---------------------------------------------------------------------------

def _generate_random_credential_type() -> str:
    """Generate a random hex-encoded credential type (8-16 bytes).

    Returns:
        A hex string representing the credential type.
    """
    # Generate 8-16 random bytes, convert to hex
    num_bytes = urand.randint(8, 16)
    random_bytes = bytes(urand.randint(0, 255) for _ in range(num_bytes))
    return random_bytes.hex().upper()


def compute_domain_id(owner: str, sequence: int) -> str:
    """Compute the domain ID from owner and sequence.

    Note: This is a placeholder. The actual implementation depends on
    how rippled computes the LedgerIndex hash for PermissionedDomain objects.

    Args:
        owner:    The domain owner's address.
        sequence: The sequence number from the creating transaction.

    Returns:
        The computed domain ID (Hash256).
    """
    # TODO: Implement actual hash computation when spec is finalized
    # For now, return a placeholder
    import hashlib
    data = f"{owner}:{sequence}".encode()
    return hashlib.sha256(data).hexdigest().upper()



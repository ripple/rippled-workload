"""Deterministic setup for the Antithesis first_* phase.

Creates the minimum ledger state needed for all transaction types to operate.
Called via the /setup endpoint before fault injection begins.

All account selection is index-based (no randomness). All parameters are
hardcoded safe values (no params.* randomizers, no should_send_faulty()).
Each creation step is individually try/excepted so a single failure cannot
block the rest of the setup sequence.
"""

import httpx
import xrpl.models
from workload import logging
from workload.models import TrustLine, Vault, Credential, PermissionedDomain, MPTokenIssuance, NFT
from antithesis.assertions import reachable
from xrpl.asyncio.transaction import autofill_and_sign
from xrpl.models.requests import SubmitOnly, Tx
from xrpl.models import IssuedCurrencyAmount
from xrpl.models.transactions import (
    TrustSet,
    MPTokenIssuanceCreate,
    VaultCreate,
    NFTokenMint,
    NFTokenMintFlag,
    CredentialCreate,
    TicketCreate,
    PermissionedDomainSet,
)
from xrpl.models.transactions.deposit_preauth import Credential as XRPLCredential

log = logging.getLogger(__name__)


async def _submit_tx(txn, client, wallet, rpc_url: str) -> dict:
    """Sign, submit, close ledger, fetch validated result.

    Works in both standalone (ledger_accept closes ledger) and
    consensus mode (ledger_accept is a no-op, consensus closes it).
    """
    signed = await autofill_and_sign(txn, client, wallet)
    tx_blob = signed.blob()
    tx_hash = signed.get_hash()

    # 1. Submit
    submit_resp = await client.request(SubmitOnly(tx_blob=tx_blob))
    engine_result = submit_resp.result.get("engine_result", "")
    if engine_result not in ("tesSUCCESS", "terQUEUED", "terPRE_SEQ"):
        return submit_resp.result

    # 2. Close ledger (standalone only; no-op in consensus)
    try:
        httpx.post(rpc_url, json={"method": "ledger_accept"}, timeout=5)
    except Exception:
        pass

    # 3. Fetch validated result with full metadata
    tx_resp = await client.request(Tx(transaction=tx_hash))
    result = tx_resp.result
    # Normalize: Tx response uses meta.TransactionResult, not engine_result
    if "engine_result" not in result and "meta" in result:
        result["engine_result"] = result["meta"].get("TransactionResult")
    return result


# Hex-encoded "setup" — used as a stable credential_type for all setup credentials.
_SETUP_CREDENTIAL_TYPE = "setup".encode("utf-8").hex()

# Hardcoded safe trust-line limit: maximum representable IOU value per the XRPL spec.
_TRUSTLINE_LIMIT = "1000000000000000"

# XRP drops for vault asset_maximum — a safe upper bound that won't conflict with
# reserve requirements on freshly-funded accounts.
_VAULT_ASSETS_MAXIMUM = "1000000000"

# Number of tickets to create per account during setup.
_TICKET_COUNT = 5


# ── Internal helpers ─────────────────────────────────────────────────


def _extract_created_id(result: dict, ledger_entry_type: str) -> str | None:
    """Return the LedgerIndex of the first CreatedNode matching ledger_entry_type."""
    for node in result.get("meta", {}).get("AffectedNodes", []):
        created = node.get("CreatedNode", {})
        if created.get("LedgerEntryType") == ledger_entry_type:
            return created.get("LedgerIndex")
    return None


def _accounts_list(workload) -> list:
    """Return a stable, ordered list of UserAccount objects.

    The list is derived from workload.accounts (a dict keyed by address).
    Ordering is insertion order, which is deterministic because accounts are
    loaded from a JSON file in a fixed sequence during Workload.__init__.
    """
    return list(workload.accounts.values())


# ── Trust lines ──────────────────────────────────────────────────────


async def _create_trust_lines(workload, accounts_list: list) -> int:
    """Create 10 trust lines between consecutive account pairs.

    Pairs: (accounts[0], accounts[1]), (accounts[2], accounts[3]), ...
    Currency codes cycle through workload.currency_codes.
    """
    created = 0
    codes = workload.currency_codes
    # 10 trust lines = 5 pairs * 2 step (pairs at indices 0-1, 2-3, ..., 8-9)
    for i in range(0, min(20, len(accounts_list) - 1), 2):
        try:
            src = accounts_list[i]
            other = accounts_list[i + 1]
            code = codes[(i // 2) % len(codes)]
            txn = TrustSet(
                account=src.address,
                limit_amount=IssuedCurrencyAmount(
                    currency=code,
                    issuer=other.address,
                    value=_TRUSTLINE_LIMIT,
                ),
            )
            result = await _submit_tx(txn, workload.client, src.wallet, workload.xrpld)
            if result.get("engine_result") == "tesSUCCESS":
                workload.trust_lines.append(
                    TrustLine(account_a=src.address, account_b=other.address, currency=code)
                )
                created += 1
                log.info("Setup: trust line %s/%s %s", src.address, other.address, code)
            else:
                log.warning(
                    "Setup: trust line %d skipped: %s", i, result.get("engine_result")
                )
        except Exception as exc:
            log.error("Setup: trust line %d failed: %s", i, exc)
    return created


# ── MPT issuances ────────────────────────────────────────────────────


async def _create_mpt_issuances(workload, accounts_list: list) -> int:
    """Create 5 MPT issuances from accounts[0..4]."""
    created = 0
    for i in range(min(5, len(accounts_list))):
        try:
            src = accounts_list[i]
            txn = MPTokenIssuanceCreate(
                account=src.address,
            )
            result = await _submit_tx(txn, workload.client, src.wallet, workload.xrpld)
            if result.get("engine_result") == "tesSUCCESS":
                mpt_id = _extract_created_id(result, "MPTokenIssuance")
                if mpt_id:
                    workload.mpt_issuances.append(
                        MPTokenIssuance(issuer=src.address, mpt_issuance_id=mpt_id)
                    )
                    created += 1
                    log.info("Setup: MPT issuance %s by %s", mpt_id, src.address)
                else:
                    log.warning("Setup: MPT issuance %d: no LedgerIndex in metadata", i)
            else:
                log.warning(
                    "Setup: MPT issuance %d skipped: %s", i, result.get("engine_result")
                )
        except Exception as exc:
            log.error("Setup: MPT issuance %d failed: %s", i, exc)
    return created


# ── Vaults ───────────────────────────────────────────────────────────


async def _create_vaults(workload, accounts_list: list) -> int:
    """Create 5 XRP vaults from accounts[10..14].

    Using accounts[10..14] avoids reserve conflicts with trust-line accounts
    (accounts[0..9]) which may have their reserve partially consumed by
    the TrustSet transactions submitted above.
    """
    created = 0
    start = 10
    for i in range(min(5, max(0, len(accounts_list) - start))):
        try:
            src = accounts_list[start + i]
            txn = VaultCreate(
                account=src.address,
                asset=xrpl.models.XRP(),
                assets_maximum=_VAULT_ASSETS_MAXIMUM,
            )
            result = await _submit_tx(txn, workload.client, src.wallet, workload.xrpld)
            if result.get("engine_result") == "tesSUCCESS":
                vault_id = _extract_created_id(result, "Vault")
                if vault_id:
                    workload.vaults.append(
                        Vault(owner=src.address, vault_id=vault_id, asset=xrpl.models.XRP())
                    )
                    created += 1
                    log.info("Setup: vault %s by %s", vault_id, src.address)
                else:
                    log.warning("Setup: vault %d: no LedgerIndex in metadata", i)
            else:
                log.warning(
                    "Setup: vault %d skipped: %s", i, result.get("engine_result")
                )
        except Exception as exc:
            log.error("Setup: vault %d failed: %s", i, exc)
    return created


# ── NFTs ─────────────────────────────────────────────────────────────


async def _create_nfts(workload, accounts_list: list) -> int:
    """Mint 5 NFTs from accounts[20..24].

    After a successful mint, the NFT is recorded in both workload.nfts (the
    shared list) and account.nfts (the per-account set), matching the pattern
    used by nft._nftoken_mint_valid.
    """
    created = 0
    start = 20
    for i in range(min(5, max(0, len(accounts_list) - start))):
        try:
            src = accounts_list[start + i]
            txn = NFTokenMint(
                account=src.address,
                nftoken_taxon=0,
                flags=NFTokenMintFlag.TF_TRANSFERABLE,
            )
            result = await _submit_tx(txn, workload.client, src.wallet, workload.xrpld)
            if result.get("engine_result") == "tesSUCCESS":
                nftoken_id = result.get("meta", {}).get("nftoken_id")
                if nftoken_id:
                    workload.nfts.append(NFT(owner=src.address, nftoken_id=nftoken_id))
                    src.nfts.add(nftoken_id)
                    created += 1
                    log.info("Setup: minted NFT %s for %s", nftoken_id, src.address)
                else:
                    log.warning("Setup: NFT %d: nftoken_id missing from metadata", i)
            else:
                log.warning(
                    "Setup: NFT %d skipped: %s", i, result.get("engine_result")
                )
        except Exception as exc:
            log.error("Setup: NFT %d failed: %s", i, exc)
    return created


# ── Credentials ──────────────────────────────────────────────────────


async def _create_credentials(workload, accounts_list: list) -> int:
    """Create 5 credentials: issuer=accounts[30], subjects=accounts[31..35].

    The credential_type is the hex encoding of the ASCII string "setup",
    giving a stable, human-readable type that won't collide with random
    credentials created during the main workload phase.
    """
    created = 0
    issuer_idx = 30
    if len(accounts_list) <= issuer_idx:
        log.warning("Setup: not enough accounts for credentials (need >= 36, have %d)", len(accounts_list))
        return 0

    issuer = accounts_list[issuer_idx]
    for i in range(min(5, len(accounts_list) - issuer_idx - 1)):
        subject_idx = issuer_idx + 1 + i
        try:
            subject = accounts_list[subject_idx]
            txn = CredentialCreate(
                account=issuer.address,
                subject=subject.address,
                credential_type=_SETUP_CREDENTIAL_TYPE,
            )
            result = await _submit_tx(txn, workload.client, issuer.wallet, workload.xrpld)
            if result.get("engine_result") == "tesSUCCESS":
                workload.credentials.append(
                    Credential(
                        issuer=issuer.address,
                        subject=subject.address,
                        credential_type=_SETUP_CREDENTIAL_TYPE,
                    )
                )
                created += 1
                log.info(
                    "Setup: credential for subject %s by %s", subject.address, issuer.address
                )
            else:
                log.warning(
                    "Setup: credential %d skipped: %s", i, result.get("engine_result")
                )
        except Exception as exc:
            log.error("Setup: credential %d failed: %s", i, exc)
    return created


# ── Tickets ──────────────────────────────────────────────────────────


async def _create_tickets(workload, accounts_list: list) -> int:
    """Create tickets for accounts[40..42], _TICKET_COUNT each.

    Tickets are stored per-account in account.tickets (a set of sequence
    numbers), matching the pattern in tickets._ticket_create_valid.
    There is no workload-level ticket list.
    """
    created = 0
    start = 40
    for i in range(min(3, max(0, len(accounts_list) - start))):
        try:
            src = accounts_list[start + i]
            txn = TicketCreate(
                account=src.address,
                ticket_count=_TICKET_COUNT,
            )
            result = await _submit_tx(txn, workload.client, src.wallet, workload.xrpld)
            if result.get("engine_result") == "tesSUCCESS":
                # Sequence numbers for the created tickets are:
                # [tx_sequence + 1, tx_sequence + ticket_count] (inclusive).
                tx_seq = result["tx_json"]["Sequence"]
                tix = set(range(tx_seq + 1, tx_seq + 1 + _TICKET_COUNT))
                src.tickets.update(tix)
                created += _TICKET_COUNT
                log.info(
                    "Setup: %d tickets for %s (seqs %d-%d)",
                    _TICKET_COUNT,
                    src.address,
                    tx_seq + 1,
                    tx_seq + _TICKET_COUNT,
                )
            else:
                log.warning(
                    "Setup: tickets %d skipped: %s", i, result.get("engine_result")
                )
        except Exception as exc:
            log.error("Setup: tickets %d failed: %s", i, exc)
    return created


# ── Permissioned domains ─────────────────────────────────────────────


async def _create_domains(workload, accounts_list: list) -> int:
    """Create 3 permissioned domains from accounts[50..52].

    Each domain is created with a single accepted credential referencing the
    setup issuer (accounts[30]) and the _SETUP_CREDENTIAL_TYPE, so that
    domains are immediately usable in conjunction with the credentials created
    above.
    """
    created = 0
    start = 50
    # Use accounts[30] as the credential issuer reference, consistent with
    # _create_credentials above.  Fall back to the domain owner if accounts[30]
    # is out of range.
    issuer_idx = 30
    if len(accounts_list) <= start + 2:
        log.warning(
            "Setup: not enough accounts for domains (need >= 53, have %d)", len(accounts_list)
        )
        return 0

    credential_issuer = accounts_list[issuer_idx] if len(accounts_list) > issuer_idx else None

    for i in range(min(3, len(accounts_list) - start)):
        try:
            src = accounts_list[start + i]
            issuer_address = credential_issuer.address if credential_issuer else src.address
            accepted = [
                XRPLCredential(
                    issuer=issuer_address,
                    credential_type=_SETUP_CREDENTIAL_TYPE,
                )
            ]
            txn = PermissionedDomainSet(
                account=src.address,
                accepted_credentials=accepted,
            )
            result = await _submit_tx(txn, workload.client, src.wallet, workload.xrpld)
            if result.get("engine_result") == "tesSUCCESS":
                for node in result.get("meta", {}).get("AffectedNodes", []):
                    created_node = node.get("CreatedNode", {})
                    if created_node.get("LedgerEntryType") == "PermissionedDomain":
                        domain_id = created_node.get("LedgerIndex", "")
                        workload.domains.append(
                            PermissionedDomain(owner=src.address, domain_id=domain_id)
                        )
                        created += 1
                        log.info("Setup: domain %s by %s", domain_id, src.address)
                        break
            else:
                log.warning(
                    "Setup: domain %d skipped: %s", i, result.get("engine_result")
                )
        except Exception as exc:
            log.error("Setup: domain %d failed: %s", i, exc)
    return created


# ── Public entry point ───────────────────────────────────────────────


async def run_setup(workload) -> dict:
    """Run deterministic setup for all tracked object types.

    Creates a stable baseline of ledger objects so that every random
    transaction endpoint has non-empty state to operate against from the
    very first Antithesis test step.

    Args:
        workload: The Workload instance (from app.py).

    Returns:
        A summary dict with counts of each object type created, e.g.::

            {
                "trust_lines": 10,
                "mpt_issuances": 5,
                "vaults": 5,
                "nfts": 5,
                "credentials": 5,
                "tickets": 15,
                "domains": 3,
            }
    """
    log.info("Setup: starting deterministic setup phase")
    accounts_list = _accounts_list(workload)
    log.info("Setup: %d accounts available", len(accounts_list))

    trust_lines = await _create_trust_lines(workload, accounts_list)
    if trust_lines:
        reachable("workload::setup_trust_lines", {"count": trust_lines})

    mpt_issuances = await _create_mpt_issuances(workload, accounts_list)
    if mpt_issuances:
        reachable("workload::setup_mpt_issuances", {"count": mpt_issuances})

    vaults = await _create_vaults(workload, accounts_list)
    if vaults:
        reachable("workload::setup_vaults", {"count": vaults})

    nfts = await _create_nfts(workload, accounts_list)
    if nfts:
        reachable("workload::setup_nfts", {"count": nfts})

    credentials = await _create_credentials(workload, accounts_list)
    if credentials:
        reachable("workload::setup_credentials", {"count": credentials})

    tickets = await _create_tickets(workload, accounts_list)
    if tickets:
        reachable("workload::setup_tickets", {"count": tickets})

    domains = await _create_domains(workload, accounts_list)
    if domains:
        reachable("workload::setup_domains", {"count": domains})

    summary = {
        "trust_lines": trust_lines,
        "mpt_issuances": mpt_issuances,
        "vaults": vaults,
        "nfts": nfts,
        "credentials": credentials,
        "tickets": tickets,
        "domains": domains,
    }
    log.info("Setup: complete — %s", summary)
    return summary

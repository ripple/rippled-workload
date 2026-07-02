"""Confidential MPT (XLS-0096) crypto over xrpl-py's ``confidential-mpt`` branch.

Proof generation (``xrpl.ext.confidential``) is excluded from the core wheel and
built only in the Antithesis image (``scripts/setup-confidential-crypto.sh``); off
image it's absent -> ``CRYPTO_AVAILABLE`` False and only faulty paths run.
"""

from __future__ import annotations

from typing import Any

from xrpl.clients import JsonRpcClient
from xrpl.models.transactions import (
    ConfidentialMPTClawback,
    ConfidentialMPTConvert,
    ConfidentialMPTConvertBack,
    ConfidentialMPTMergeInbox,
    ConfidentialMPTSend,
)

# Proof generation is excluded from the core wheel, so the type env can't resolve
# xrpl.ext.confidential; treat it as an optional, dynamically-typed dependency.
_conf: Any
try:
    from xrpl.ext import confidential as _conf  # type: ignore[no-redef]
except ImportError:
    _conf = None

CRYPTO_AVAILABLE: bool = bool(_conf.MPT_CRYPTO_AVAILABLE) if _conf is not None else False

# Shared because secp256k1 allocation is expensive; None when native lib absent.
_crypto = _conf.MPTCrypto() if CRYPTO_AVAILABLE else None


# ── Wire-format sizes (bytes) ────────────────────────────────────────
# rippled enforces these in preflight (wrong length -> tem*, no failure bucket);
# branch models enforce the same, so faulty bases build AND clear preflight here.
CIPHERTEXT_SIZE = 66  # ElGamal c1 || c2 (two compressed points)
ENCRYPTION_KEY_SIZE = 33  # compressed ElGamal pubkey (HolderEncryptionKey)
BLINDING_FACTOR_SIZE = 32
COMMITMENT_SIZE = 33  # Pedersen commitment (one compressed point)
SCHNORR_PROOF_SIZE = 64  # Convert ZKProof (Schnorr PoK)
CLAWBACK_PROOF_SIZE = 64  # Clawback ZKProof (compact sigma)
SEND_PROOF_SIZE = 946  # Send ZKProof (compact sigma 192 + double bulletproof 754)
CONVERT_BACK_PROOF_SIZE = 816  # ConvertBack ZKProof (compact sigma 128 + bulletproof 688)


# ── Sync client for the builders ──────────────────────────────────────
# prepare_confidential_* need a blocking client; handlers hold an async one, so
# derive a sync one from its URL and cache it.
_sync_clients: dict[str, JsonRpcClient] = {}


def sync_client(url: str) -> JsonRpcClient:
    client = _sync_clients.get(url)
    if client is None:
        client = JsonRpcClient(url)
        _sync_clients[url] = client
    return client


def account_sequence(url: str, address: str) -> int:
    """Current Sequence — the builder binds it into the proof, so submit must stamp
    the same value (a different autofilled one -> tecBAD_PROOF)."""
    from xrpl.models.requests import AccountInfo

    resp = sync_client(url).request(AccountInfo(account=address))
    return int(resp.result["account_data"]["Sequence"])


def generate_keypair() -> tuple[str, str]:
    assert _crypto is not None  # callers gate on CRYPTO_AVAILABLE
    return _crypto.generate_keypair()


def issuer_encrypted_balance(url: str, holder_address: str, mpt_id: str) -> str:
    """Holder MPToken's ``IssuerEncryptedBalance`` (Clawback proof input), or ``""``."""
    from xrpl.models.requests import LedgerEntry
    from xrpl.models.requests.ledger_entry import MPToken

    resp = sync_client(url).request(
        LedgerEntry(mptoken=MPToken(account=holder_address, mpt_issuance_id=mpt_id))
    )
    return resp.result.get("node", {}).get("IssuerEncryptedBalance", "")


# ── Builders (real proofs, valid path only) ──────────────────────────
# ElGamal keys are explicit params; builders query mutable ledger state, prove,
# encrypt, and return an UNSIGNED model for submit_tx.


def build_merge_inbox(url: str, wallet: object, mpt_id: str) -> ConfidentialMPTMergeInbox:
    return _conf.prepare_confidential_merge_inbox(sync_client(url), wallet, mpt_id)


def build_convert(
    url: str,
    wallet: object,
    mpt_id: str,
    amount: int,
    issuer_pubkey: str,
    holder_privkey: str | None = None,
    holder_pubkey: str | None = None,
) -> ConfidentialMPTConvert:
    return _conf.prepare_confidential_convert(
        sync_client(url), wallet, mpt_id, int(amount), issuer_pubkey, holder_privkey, holder_pubkey
    )


def build_convert_back(
    url: str,
    wallet: object,
    mpt_id: str,
    amount: int,
    holder_privkey: str,
    holder_pubkey: str,
    issuer_pubkey: str,
) -> ConfidentialMPTConvertBack:
    return _conf.prepare_confidential_convert_back(
        sync_client(url), wallet, mpt_id, int(amount), holder_privkey, holder_pubkey, issuer_pubkey
    )


def build_send(
    url: str,
    sender_wallet: object,
    receiver_address: str,
    mpt_id: str,
    amount: int,
    sender_privkey: str,
    sender_pubkey: str,
    receiver_pubkey: str,
    issuer_pubkey: str,
) -> ConfidentialMPTSend:
    return _conf.prepare_confidential_send(
        sync_client(url),
        sender_wallet,
        receiver_address,
        mpt_id,
        int(amount),
        sender_privkey,
        sender_pubkey,
        receiver_pubkey,
        issuer_pubkey,
    )


def build_clawback(
    url: str,
    issuer_wallet: object,
    holder_address: str,
    mpt_id: str,
    amount: int,
    issuer_privkey: str,
    issuer_pubkey: str,
    issuer_encrypted_balance_hex: str,
) -> ConfidentialMPTClawback:
    return _conf.prepare_confidential_clawback(
        sync_client(url),
        issuer_wallet,
        holder_address,
        mpt_id,
        int(amount),
        issuer_privkey,
        issuer_pubkey,
        issuer_encrypted_balance_hex,
    )

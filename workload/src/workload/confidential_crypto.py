"""Confidential MPT cryptographic helpers for the Antithesis workload.

Thin wrapper around ``xrpl.core.confidential.MPTCrypto`` — single shared
secp256k1 context, deterministic blinding via AntithesisRandom, and helper
functions for proof generation used by ``transactions/confidential_mpt.py``
and ``setup.py``.
"""

from __future__ import annotations

try:
    from xrpl.core.confidential import MPTCrypto
    from xrpl.core.confidential import context as xrpl_context

    # Single shared crypto context — secp256k1 allocation is expensive.
    _crypto = MPTCrypto()
    CRYPTO_AVAILABLE = True
except Exception:
    _crypto = None  # type: ignore[assignment]
    xrpl_context = None  # type: ignore[assignment]
    CRYPTO_AVAILABLE = False


# ---------------------------------------------------------------------------
# Keypair helpers
# ---------------------------------------------------------------------------


def generate_keypair() -> tuple[str, str]:
    """Return ``(privkey_hex, pubkey_hex)``."""
    return _crypto.generate_keypair()


def generate_keypair_with_pok(context_id: str | None = None) -> tuple[str, str, str]:
    """Return ``(privkey_hex, pubkey_hex, pok_hex)``."""
    return _crypto.generate_keypair_with_pok(context_id)


# ---------------------------------------------------------------------------
# Blinding factor
# ---------------------------------------------------------------------------

# Cache a throwaway pubkey for blinding-factor generation.
_TEMP_PRIV, _TEMP_PUB = _crypto.generate_keypair() if CRYPTO_AVAILABLE else ("", "")


def generate_blinding_factor() -> str:
    """Generate a 32-byte blinding factor as hex string."""
    _, _, bf = _crypto.encrypt(_TEMP_PUB, 0)
    return bf


# ---------------------------------------------------------------------------
# ElGamal encrypt / decrypt
# ---------------------------------------------------------------------------


def _to_uint64(amount: int) -> int:
    """Coerce to uint64 — negative values wrap modulo 2**64."""
    return int(amount) & 0xFFFFFFFFFFFFFFFF


def encrypt(pubkey_hex: str, amount: int, blinding_factor: str | None = None) -> str:
    """Encrypt *amount* under *pubkey_hex*. Returns 132-char ciphertext (c1‖c2)."""
    c1, c2, _ = _crypto.encrypt(pubkey_hex, _to_uint64(amount), blinding_factor)
    return c1 + c2


def decrypt(privkey_hex: str, ciphertext: str) -> int:
    """Decrypt a 132-char hex ciphertext. Returns the plaintext amount."""
    c1, c2 = ciphertext[:66], ciphertext[66:]
    return _crypto.decrypt(privkey_hex, c1, c2)


# ---------------------------------------------------------------------------
# Context hashes
# ---------------------------------------------------------------------------


def convert_context_hash(account_hex: str, seq: int, mpt_id: str) -> str:
    return xrpl_context.compute_convert_context_hash(
        bytes.fromhex(account_hex),
        int(seq),
        bytes.fromhex(mpt_id),
    )


def send_context_hash(
    account_hex: str,
    seq: int,
    mpt_id: str,
    dest_hex: str,
    version: int,
) -> str:
    return xrpl_context.compute_send_context_hash(
        bytes.fromhex(account_hex),
        int(seq),
        bytes.fromhex(mpt_id),
        bytes.fromhex(dest_hex),
        int(version),
    )


def convert_back_context_hash(
    account_hex: str,
    seq: int,
    mpt_id: str,
    version: int,
) -> str:
    return xrpl_context.compute_convert_back_context_hash(
        bytes.fromhex(account_hex),
        int(seq),
        bytes.fromhex(mpt_id),
        int(version),
    )


def clawback_context_hash(
    issuer_hex: str,
    seq: int,
    mpt_id: str,
    holder_hex: str,
) -> str:
    return xrpl_context.compute_clawback_context_hash(
        bytes.fromhex(issuer_hex),
        int(seq),
        bytes.fromhex(mpt_id),
        bytes.fromhex(holder_hex),
    )


# ---------------------------------------------------------------------------
# Pedersen commitments
# ---------------------------------------------------------------------------


def pedersen_commitment(amount: int, blinding_factor: str) -> str:
    return _crypto.create_pedersen_commitment(_to_uint64(amount), blinding_factor)


# ---------------------------------------------------------------------------
# ZK proofs
# ---------------------------------------------------------------------------


def pok_proof(privkey: str, pubkey: str, context_hash: str) -> str:
    """Schnorr proof of knowledge (Convert / key registration)."""
    return _crypto.generate_pok(privkey, pubkey, context_hash)


def send_proof(
    sender_privkey: str,
    sender_pubkey: str,
    amount: int,
    current_balance: int,
    participants: list[tuple[str, str]],
    tx_blinding_factor: str,
    context_hash: str,
    amount_commitment: str,
    balance_commitment: str,
    balance_blinding: str,
    sender_balance_encrypted: str,
) -> str:
    """Combined Compact Sigma + Bulletproof proof for ConfidentialMPTSend."""
    return _crypto.create_confidential_send_proof(
        sender_privkey=sender_privkey,
        sender_pubkey=sender_pubkey,
        amount=_to_uint64(amount),
        sender_current_balance=_to_uint64(current_balance),
        participants=participants,
        tx_blinding_factor=tx_blinding_factor,
        context_hash=context_hash,
        amount_commitment=amount_commitment,
        balance_commitment=balance_commitment,
        balance_blinding=balance_blinding,
        sender_balance_encrypted=sender_balance_encrypted,
    )


def convert_back_proof(
    holder_privkey: str,
    holder_pubkey: str,
    amount: int,
    current_balance: int,
    context_hash: str,
    balance_commitment: str,
    balance_blinding: str,
    holder_balance_encrypted: str,
) -> str:
    """Compact Sigma proof for ConfidentialMPTConvertBack."""
    return _crypto.create_confidential_convert_back_proof(
        holder_privkey=holder_privkey,
        holder_pubkey=holder_pubkey,
        amount=_to_uint64(amount),
        current_balance=_to_uint64(current_balance),
        context_hash=context_hash,
        balance_commitment=balance_commitment,
        balance_blinding=balance_blinding,
        holder_balance_encrypted=holder_balance_encrypted,
    )


def clawback_proof(
    issuer_privkey: str,
    issuer_pubkey: str,
    amount: int,
    context_hash: str,
    issuer_encrypted_balance: str,
) -> str:
    """Equality proof for ConfidentialMPTClawback."""
    if int(amount) == 0:
        # mpt-crypto refuses to build a proof for amount=0;
        # return a dummy 64-byte buffer so callers can submit and let
        # rippled return temBAD_AMOUNT.
        return "00" * 64
    return _crypto.create_confidential_clawback_proof(
        issuer_privkey=issuer_privkey,
        issuer_pubkey=issuer_pubkey,
        amount=_to_uint64(amount),
        context_hash=context_hash,
        issuer_encrypted_balance=issuer_encrypted_balance,
    )


# ---------------------------------------------------------------------------
# Account ID → hex helper
# ---------------------------------------------------------------------------


def account_to_hex(classic_address: str) -> str:
    """Convert an r-address to its 20-byte AccountID hex (upper-case)."""
    return xrpl_context.decode_classic_address(classic_address).hex().upper()

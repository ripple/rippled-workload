"""Centralized random parameter generators for workload transactions.

Every tunable transaction parameter lives here. Call these functions
at the point of use — never cache the return value.
"""

from workload.randoms import randint, choice


# ── Fees ─────────────────────────────────────────────────────────────
def fee() -> str:
    """Transaction fee in drops."""
    return str(randint(10, 100))


# ── Payments ─────────────────────────────────────────────────────────
def payment_amount() -> str:
    """Payment amount in drops."""
    return str(randint(1_000, 10_000_000))


# ── NFTokens ─────────────────────────────────────────────────────────
def nft_taxon() -> int:
    return randint(0, 10)


def nft_transfer_fee() -> int:
    """Transfer fee in basis points (0-50%)."""
    return randint(0, 5000)


def nft_memo() -> str:
    """Human-readable memo string (not yet hex-encoded)."""
    return f"nft-memo-{randint(0, 1_000_000)}"


def nft_offer_amount() -> str:
    """Amount in drops for NFToken offers."""
    return str(randint(1_000, 10_000_000))


# ── Tickets ──────────────────────────────────────────────────────────
def ticket_count() -> int:
    return randint(1, 10)


# ── Batch ────────────────────────────────────────────────────────────
def batch_size() -> int:
    """Number of inner transactions in a batch."""
    return randint(1, 16)


def batch_inner_amount() -> str:
    """Amount for each inner batch transaction in drops."""
    return str(randint(1_000, 10_000_000))

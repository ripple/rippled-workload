"""Centralized random parameter generators for workload transactions.

Every tunable transaction parameter lives here. Call these functions
at the point of use — never cache the return value.
"""

from workload.randoms import choice, randint, random


# ── Fuzzing ──────────────────────────────────────────────────────────
def should_send_faulty() -> bool:
    """Let Antithesis decide whether to bypass precondition checks
    and send a deliberately invalid transaction."""
    return random() < 0.01


def fake_account() -> str:
    """Generate a valid-format but non-existent XRPL account address."""
    from xrpl.wallet import Wallet

    return Wallet.create().address


def fake_id() -> str:
    """Generate a random 64-char hex string (fake object ID)."""
    return bytes(randint(0, 255) for _ in range(32)).hex().upper()


# ── Fees ─────────────────────────────────────────────────────────────
def fee() -> str:
    """Transaction fee in drops."""
    return str(randint(10, 100))


# ── Payments ─────────────────────────────────────────────────────────
def payment_amount() -> str:
    """Payment amount in drops."""
    return str(randint(1_000, 10_000_000))


# ── Trust Lines & IOUs ────────────────────────────────────────────────
CURRENCY_CODES = ["USD", "EUR", "GBP", "JPY", "BTC", "ETH", "XAU", "CNY"]


def currency_code() -> str:
    """Random 3-letter currency code."""
    return choice(CURRENCY_CODES)


def trustline_limit() -> str:
    """Trust line limit value. Ranges from 0 to 100k to overlap with
    typical IOU payment amounts (1-10k), ensuring we hit limit violations."""
    return str(randint(0, 100_000))


def iou_amount() -> str:
    """IOU payment amount (value, not drops)."""
    return str(randint(1, 10_000))


# ── NFTokens ─────────────────────────────────────────────────────────
def nft_taxon() -> int:
    return randint(0, 10)


def nft_transfer_fee() -> int:
    """Transfer fee in 1/10th basis points (0-50000 = 0-50%)."""
    return randint(0, 50000)


def nft_memo() -> str:
    """Human-readable memo string (not yet hex-encoded)."""
    return f"nft-memo-{randint(0, 1_000_000)}"


def nft_offer_amount() -> str:
    """Amount in drops for NFToken offers."""
    return str(randint(1_000, 10_000_000))


# ── Tickets ──────────────────────────────────────────────────────────
def ticket_count() -> int:
    """1-250 tickets per TicketCreate."""
    return randint(1, 250)


# ── Batch ────────────────────────────────────────────────────────────
def batch_size() -> int:
    """Number of inner transactions in a batch."""
    return randint(1, 16)


def batch_inner_amount() -> str:
    """Amount for each inner batch transaction in drops."""
    return str(randint(1_000, 10_000_000))


# ── Credentials ──────────────────────────────────────────────────────
def credential_type() -> str:
    """Random hex-encoded credential type (1-64 bytes → 2-128 hex chars)."""
    length = randint(1, 64)
    return bytes(randint(0, 255) for _ in range(length)).hex()


def credential_uri() -> str:
    """Random hex-encoded URI for credentials (max 256 hex chars = 128 bytes)."""
    length = randint(5, 128)
    return bytes(randint(0, 255) for _ in range(length)).hex()


def credential_expiration_offset() -> int:
    """Seconds from now until credential expires."""
    return randint(3600, 86400 * 30)  # 1 hour to 30 days


# ── Vaults ───────────────────────────────────────────────────────────
def vault_deposit_amount() -> str:
    """Deposit amount in drops for vaults."""
    return str(randint(1_000_000, 100_000_000))


def vault_withdraw_amount() -> str:
    """Withdraw amount in drops for vaults."""
    return str(randint(100_000, 50_000_000))


def vault_data() -> str:
    """Random hex-encoded vault metadata (up to 256 bytes → 512 hex chars)."""
    length = randint(1, 256)
    return bytes(randint(0, 255) for _ in range(length)).hex()


def vault_assets_maximum() -> str:
    """Maximum vault capacity in drops."""
    return str(randint(100_000_000, 10_000_000_000))


# ── Permissioned Domains ─────────────────────────────────────────────
def domain_credential_count() -> int:
    """Number of accepted credentials in a permissioned domain (1-10)."""
    return randint(1, 10)


# ── NFToken Modify ───────────────────────────────────────────────────
def nft_uri() -> str:
    """Random hex-encoded URI for NFTs (max 512 hex chars = 256 bytes)."""
    length = randint(5, 256)
    return bytes(randint(0, 255) for _ in range(length)).hex()


# ── MPToken ──────────────────────────────────────────────────────────
def mpt_amount() -> str:
    """MPT payment/transfer amount."""
    return str(randint(1, 10_000))


def mpt_maximum_amount() -> str:
    """Max 9,223,372,036,854,775,807 per spec."""
    return str(randint(1_000_000, 9_223_372_036_854_775_807))


def mpt_metadata() -> str:
    """Random hex-encoded MPToken metadata (max 1024 bytes)."""
    length = randint(1, 1024)
    return bytes(randint(0, 255) for _ in range(length)).hex()


# ── Lending Protocol ─────────────────────────────────────────────────
def loan_broker_management_fee_rate() -> int:
    """1/10th basis point fee (0-10000 = 0-10%)."""
    return randint(0, 10000)


def loan_broker_cover_rate_minimum() -> int:
    """1/10th basis point cover rate (0-100000 = 0-100%)."""
    return randint(0, 100000)


def loan_broker_cover_rate_liquidation() -> int:
    """1/10th basis point liquidation rate (0-100000 = 0-100%)."""
    return randint(0, 100000)


def loan_broker_debt_maximum() -> str:
    return str(randint(1_000_000, 1_000_000_000))


def loan_broker_data() -> str:
    """Random hex-encoded broker metadata (max 256 bytes)."""
    length = randint(1, 256)
    return bytes(randint(0, 255) for _ in range(length)).hex()


def loan_principal() -> str:
    """Loan principal amount in drops."""
    return str(randint(100_000, 50_000_000))


def loan_interest_rate() -> int:
    """Annualized interest rate in 1/10th basis points (0-100000 = 0-100%)."""
    return randint(0, 100000)


def loan_payment_total() -> int:
    """Total number of loan payments."""
    return randint(1, 24)


def loan_payment_interval() -> int:
    """Seconds between payments (min 60)."""
    return randint(60, 86400)


def loan_grace_period(payment_interval: int) -> int:
    """Seconds after due date before default (min 60, max ≤ payment_interval)."""
    return randint(60, max(60, payment_interval))


def loan_cover_deposit_amount() -> str:
    """First loss capital deposit in drops."""
    return str(randint(100_000, 10_000_000))


def loan_pay_amount() -> str:
    """Loan payment amount in drops."""
    return str(randint(10_000, 5_000_000))



# ── Escrow ──────────────────────────────────────────────────────────
RIPPLE_EPOCH_OFFSET = 946_684_800


def escrow_amount() -> str:
    """Escrow amount in drops (0.1-50 XRP)."""
    return str(randint(100_000, 50_000_000))


def _ripple_now() -> int:
    import time
    return int(time.time()) - RIPPLE_EPOCH_OFFSET


def escrow_finish_after() -> int:
    """Ripple timestamp a short time in the future (1-120s)."""
    return _ripple_now() + randint(1, 120)


def escrow_cancel_after(finish_after: int) -> int:
    """Ripple timestamp after finish_after (3-600s later)."""
    return finish_after + randint(3, 600)


def escrow_condition_pair() -> tuple[str, str]:
    """Generate a PREIMAGE-SHA-256 crypto-condition (condition, fulfillment) pair.

    Returns (condition_hex, fulfillment_hex) where both are uppercased hex strings.
    """
    import hashlib
    preimage = bytes(randint(0, 255) for _ in range(32))
    # Fulfillment: DER-encoded PREIMAGE-SHA-256
    fulfillment_bytes = bytes([0xA0, len(preimage) + 2, 0x80, len(preimage)]) + preimage
    # Condition: type 0 (preimage), SHA-256 fingerprint, max fulfillment length
    fingerprint = hashlib.sha256(preimage).digest()
    condition_bytes = bytes([0xA0, 0x25, 0x80, 0x20]) + fingerprint + bytes([0x81, 0x01, len(preimage)])
    return condition_bytes.hex().upper(), fulfillment_bytes.hex().upper()
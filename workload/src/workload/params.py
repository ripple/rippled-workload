"""Centralized random parameter generators; call at point of use, never cache."""

from workload.randoms import choice, randint, random


# ── Fuzzing ──────────────────────────────────────────────────────────
def should_send_faulty() -> bool:
    return random() < 0.01


def fake_account() -> str:
    from xrpl.wallet import Wallet

    address: str = Wallet.create().address
    return address


def fake_id() -> str:
    return bytes(randint(0, 255) for _ in range(32)).hex().upper()


def fake_mpt_id() -> str:
    """MPTokenIssuanceID is 24 bytes (4-byte seq + 20-byte issuer), unlike fake_id's 32."""
    return bytes(randint(0, 255) for _ in range(24)).hex().upper()


# ── Fees ─────────────────────────────────────────────────────────────
def fee() -> str:
    return str(randint(10, 100))


# ── Payments ─────────────────────────────────────────────────────────
def payment_amount() -> str:
    return str(randint(1_000, 10_000_000))


# ── Trust Lines & IOUs ────────────────────────────────────────────────
CURRENCY_CODES = ["USD", "EUR", "GBP", "JPY", "BTC", "ETH", "XAU", "CNY"]


def currency_code() -> str:
    return choice(CURRENCY_CODES)


def trustline_limit() -> str:
    """Range overlaps IOU amounts (1-10k) to hit limit violations."""
    return str(randint(0, 100_000))


def iou_amount() -> str:
    return str(randint(1, 10_000))


# ── Offers / DEX ─────────────────────────────────────────────────────
def offer_xrp_drops() -> str:
    return str(randint(100_000, 10_000_000))


def offer_iou_value() -> str:
    return str(randint(1, 50))


def mpt_offer_value() -> str:
    return str(randint(1, 1000))


# ── NFTokens ─────────────────────────────────────────────────────────
def nft_taxon() -> int:
    return randint(0, 10)


def nft_transfer_fee() -> int:
    """1/10th basis points (0-50000 = 0-50%)."""
    return randint(0, 50000)


def nft_memo() -> str:
    return f"nft-memo-{randint(0, 1_000_000)}"


def nft_offer_amount() -> str:
    return str(randint(1_000, 10_000_000))


# ── Tickets ──────────────────────────────────────────────────────────
def ticket_count() -> int:
    """1-250 per TicketCreate."""
    return randint(1, 250)


# ── Batch ────────────────────────────────────────────────────────────
def batch_size() -> int:
    """Min 2 per xrpl-py, max 8 per rippled."""
    return randint(2, 8)


def batch_inner_amount() -> str:
    return str(randint(1_000, 10_000_000))


# ── Credentials ──────────────────────────────────────────────────────
def credential_type() -> str:
    """1-64 bytes per spec."""
    length = randint(1, 64)
    return bytes(randint(0, 255) for _ in range(length)).hex()


def credential_uri() -> str:
    """Max 128 bytes per spec."""
    length = randint(5, 128)
    return bytes(randint(0, 255) for _ in range(length)).hex()


def credential_expiration_offset() -> int:
    return randint(3600, 86400 * 30)  # 1 hour to 30 days


# ── Vaults ───────────────────────────────────────────────────────────
def vault_deposit_amount() -> str:
    return str(randint(1_000_000, 100_000_000))


def vault_withdraw_amount() -> str:
    return str(randint(100_000, 50_000_000))


def vault_data() -> str:
    """Max 256 bytes per spec."""
    length = randint(1, 256)
    return bytes(randint(0, 255) for _ in range(length)).hex()


def vault_assets_maximum() -> str:
    return str(randint(100_000_000, 10_000_000_000))


# ── Permissioned Domains ─────────────────────────────────────────────
def domain_credential_count() -> int:
    """1-10 accepted credentials per domain."""
    return randint(1, 10)


def zero_domain_id() -> str:
    """All-zero DomainID: temMALFORMED under fixCleanup3_2_0; xrpl-py accepts it."""
    return "0" * 64


def should_update_domain() -> bool:
    return random() < 0.3


# ── NFToken Modify ───────────────────────────────────────────────────
def nft_uri() -> str:
    """Max 256 bytes per spec."""
    length = randint(5, 256)
    return bytes(randint(0, 255) for _ in range(length)).hex()


# ── MPToken ──────────────────────────────────────────────────────────
def mpt_amount() -> str:
    return str(randint(1, 10_000))


def mpt_maximum_amount() -> str:
    """Max 9,223,372,036,854,775,807 per spec."""
    return str(randint(1_000_000, 9_223_372_036_854_775_807))


def mpt_metadata() -> str:
    """Max 1024 bytes per spec."""
    length = randint(1, 1024)
    return bytes(randint(0, 255) for _ in range(length)).hex()


# ── AMM ─────────────────────────────────────────────────────────────
def amm_trading_fee() -> int:
    """1/100,000th (0-1000 = 0-1%)."""
    return randint(0, 1000)


def amm_deposit_amount() -> str:
    """Within typical 10k balance."""
    return str(randint(100, 5_000))


def amm_withdraw_amount() -> str:
    return str(randint(100, 50_000))


def amm_lp_token_amount() -> str:
    return str(randint(1, 10_000))


def amm_bid_min() -> str:
    return str(randint(1, 1_000))


def amm_bid_max() -> str:
    return str(randint(1_000, 10_000))


def amm_vote_fee() -> int:
    return randint(0, 1000)


def amm_xrp_amount() -> str:
    return str(randint(10_000_000, 1_000_000_000))


# ── Lending Protocol ─────────────────────────────────────────────────
def loan_broker_management_fee_rate() -> int:
    """1/10th basis point (0-10000 = 0-10%)."""
    return randint(0, 10000)


def loan_broker_cover_rate_minimum() -> int:
    """1/10th basis point (0-100000 = 0-100%)."""
    return randint(0, 100000)


def loan_broker_cover_rate_liquidation() -> int:
    """1/10th basis point (0-100000 = 0-100%)."""
    return randint(0, 100000)


def loan_broker_debt_maximum() -> str:
    return str(randint(1_000_000, 1_000_000_000))


def loan_broker_data() -> str:
    """Max 256 bytes per spec."""
    length = randint(1, 256)
    return bytes(randint(0, 255) for _ in range(length)).hex()


def loan_principal() -> str:
    return str(randint(100_000, 50_000_000))


def loan_interest_rate() -> int:
    """1/10th basis points (0-100000 = 0-100%)."""
    return randint(0, 100000)


def loan_payment_total() -> int:
    return randint(1, 24)


def loan_payment_interval() -> int:
    """Min 60."""
    return randint(60, 86400)


def loan_grace_period(payment_interval: int) -> int:
    """Min 60, max ≤ payment_interval."""
    return randint(60, max(60, payment_interval))


def loan_cover_deposit_amount() -> str:
    return str(randint(100_000, 10_000_000))


def loan_pay_amount() -> str:
    return str(randint(10_000, 5_000_000))


# ── Escrow ──────────────────────────────────────────────────────────
RIPPLE_EPOCH_OFFSET = 946_684_800


def escrow_amount() -> str:
    return str(randint(100_000, 50_000_000))


def _ripple_now() -> int:
    import time

    return int(time.time()) - RIPPLE_EPOCH_OFFSET


def escrow_finish_after() -> int:
    return _ripple_now() + randint(1, 120)


def escrow_cancel_after(finish_after: int) -> int:
    return finish_after + randint(3, 600)


def escrow_condition_pair() -> tuple[str, str]:
    """PREIMAGE-SHA-256 (condition_hex, fulfillment_hex), both uppercased."""
    import hashlib

    preimage = bytes(randint(0, 255) for _ in range(32))
    fulfillment_bytes = bytes([0xA0, len(preimage) + 2, 0x80, len(preimage)]) + preimage
    fingerprint = hashlib.sha256(preimage).digest()
    condition_bytes = (
        bytes([0xA0, 0x25, 0x80, 0x20]) + fingerprint + bytes([0x81, 0x01, len(preimage)])
    )
    return condition_bytes.hex().upper(), fulfillment_bytes.hex().upper()


# ── Checks ──────────────────────────────────────────────────────────


def check_send_max() -> str:
    return str(randint(1_000_000, 100_000_000))


def check_cash_amount(send_max: str) -> str:
    """≤ send_max."""
    max_val = int(send_max)
    return str(randint(1, max_val))


# ── Payment Channels ────────────────────────────────────────────────


def channel_amount() -> str:
    return str(randint(1_000_000, 100_000_000))


def channel_settle_delay() -> int:
    return randint(60, 3600)


def channel_fund_amount() -> str:
    return str(randint(100_000, 10_000_000))


def channel_claim_balance(channel_amount: str) -> str:
    """≤ channel amount."""
    max_val = int(channel_amount)
    return str(randint(1, max_val))


# ── Clawback ────────────────────────────────────────────────────────


def clawback_iou_amount() -> str:
    """Within typical 10k holder balance."""
    return str(randint(1, 1_000))


def clawback_mpt_amount() -> str:
    """Within typical 10k holder balance."""
    return str(randint(1, 1_000))


# ── DID ───────────────────────────────────────────────────────────────

_HEX_CHARS = "0123456789ABCDEF"


def did_hex_field() -> str:
    """Even-length hex, max 128 bytes; for DID uri/data/did_document."""
    r = random()
    if r < 0.80:
        n = randint(1, 32)
    elif r < 0.95:
        n = randint(33, 100)
    else:
        n = randint(101, 128)  # near the 128-byte limit
    return "".join(choice(_HEX_CHARS) for _ in range(n * 2))

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
    """Number of inner transactions in a batch (min 2 per xrpl-py, max 8 per rippled)."""
    return randint(2, 8)


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


# ── AMM ─────────────────────────────────────────────────────────────
def amm_trading_fee() -> int:
    """Trading fee in 1/100,000th (0-1000 = 0-1%)."""
    return randint(0, 1000)


def amm_deposit_amount() -> str:
    """AMM deposit amount for IOU tokens (within typical 10k balance)."""
    return str(randint(100, 5_000))


def amm_withdraw_amount() -> str:
    """AMM withdrawal amount for IOU tokens."""
    return str(randint(100, 50_000))


def amm_lp_token_amount() -> str:
    """LP token amount for deposits/withdrawals/bids."""
    return str(randint(1, 10_000))


def amm_bid_min() -> str:
    """Minimum bid price for auction slot."""
    return str(randint(1, 1_000))


def amm_bid_max() -> str:
    """Maximum bid price for auction slot."""
    return str(randint(1_000, 10_000))


def amm_vote_fee() -> int:
    """Fee value for AMM vote (0-1000)."""
    return randint(0, 1000)


def amm_xrp_amount() -> str:
    """XRP amount in drops for AMM pools."""
    return str(randint(10_000_000, 1_000_000_000))


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
    condition_bytes = (
        bytes([0xA0, 0x25, 0x80, 0x20]) + fingerprint + bytes([0x81, 0x01, len(preimage)])
    )
    return condition_bytes.hex().upper(), fulfillment_bytes.hex().upper()


# ── Checks ──────────────────────────────────────────────────────────


def check_send_max() -> str:
    """Check send_max in drops (1-100 XRP)."""
    return str(randint(1_000_000, 100_000_000))


def check_cash_amount(send_max: str) -> str:
    """Cash amount ≤ send_max."""
    max_val = int(send_max)
    return str(randint(1, max_val))


# ── Payment Channels ────────────────────────────────────────────────


def channel_amount() -> str:
    """Payment channel amount in drops (1-100 XRP)."""
    return str(randint(1_000_000, 100_000_000))


def channel_settle_delay() -> int:
    """Settle delay in seconds (60-3600)."""
    return randint(60, 3600)


def channel_fund_amount() -> str:
    """Additional XRP to add to a channel in drops (0.1-10 XRP)."""
    return str(randint(100_000, 10_000_000))


def channel_claim_balance(channel_amount: str) -> str:
    """Claim balance ≤ channel amount."""
    max_val = int(channel_amount)
    return str(randint(1, max_val))


# ── Clawback ────────────────────────────────────────────────────────


def clawback_iou_amount() -> str:
    """IOU clawback value (1-1000, within typical 10k holder balance)."""
    return str(randint(1, 1_000))


def clawback_mpt_amount() -> str:
    """MPT clawback value (1-1000, within typical 10k holder balance)."""
    return str(randint(1, 1_000))


# ── DID ───────────────────────────────────────────────────────────────

_HEX_CHARS = "0123456789ABCDEF"


def did_hex_field() -> str:
    """Random even-length hex string, max 256 hex chars (128 bytes).

    Used for DID ``uri``, ``data``, and ``did_document`` fields.
    """
    r = random()
    if r < 0.80:
        n = randint(1, 32)
    elif r < 0.95:
        n = randint(33, 100)
    else:
        n = randint(101, 128)  # near the 256-hex-char / 128-byte limit
    # n is number of BYTES; each byte = 2 hex chars → always even length
    return "".join(choice(_HEX_CHARS) for _ in range(n * 2))


# ── Price Oracle (XLS-47) ─────────────────────────────────────────────


def oracle_document_id() -> int:
    """Random OracleDocumentID (uint32)."""
    return randint(0, 2**32 - 1)


def oracle_provider() -> str:
    """Random Provider string, hex-encoded (up to 256 hex chars)."""
    providers = [
        "chainlink",
        "band_protocol",
        "pyth_network",
        "switchboard",
        "dia_data",
        "api3",
        "redstone",
        "uma_protocol",
    ]
    raw = choice(providers) + "_" + str(randint(0, 999))
    return raw.encode().hex().upper()


def oracle_asset_class() -> str:
    """Random AssetClass string, hex-encoded (up to 16 hex chars)."""
    raw = choice(["currency", "commodity", "stock", "crypto"])
    return raw.encode().hex().upper()


def oracle_base_asset() -> str:
    """Random base asset for a PriceData entry."""
    return choice(["XRP", "USD", "EUR", "BTC", "ETH", "JPY", "GBP"])


def oracle_quote_asset() -> str:
    """Random quote asset for a PriceData entry."""
    return choice(["USD", "EUR", "XRP", "BTC", "ETH", "JPY", "GBP"])


def oracle_asset_price() -> int:
    """Random AssetPrice (uint64 but keep reasonable)."""
    return randint(1, 10_000_000)


def oracle_scale() -> int:
    """Random Scale (0-10)."""
    return randint(0, 10)


def oracle_price_data_count() -> int:
    """Number of PriceData entries (1-5, spec allows up to 10)."""
    return randint(1, 5)


def oracle_last_update_time() -> int:
    """Seconds since XRPL epoch (Jan 1 2000). Use current-ish time."""
    import time

    # XRPL epoch offset from Unix epoch
    xrpl_epoch = 946684800
    return int(time.time()) - xrpl_epoch


# ── Confidential MPT (XLS-0096) ─────────────────────────────────────

# Length constants (hex characters)
_CIPHERTEXT_HEX_LEN = 132  # 66 bytes — ElGamal ciphertext
_COMMITMENT_HEX_LEN = 66  # 33 bytes — Pedersen commitment
_BLINDING_FACTOR_HEX_LEN = 64  # 32 bytes
_SCHNORR_PROOF_HEX_LEN = 128  # 64 bytes — Convert / Clawback proof
_SEND_PROOF_HEX_LEN = 1892  # 946 bytes — Send proof
_CONVERT_BACK_PROOF_HEX_LEN = 1632  # 816 bytes — ConvertBack proof
_ENCRYPTION_KEY_HEX_LEN = 66  # 33 bytes — holder ElGamal public key


def _garbage_hex(hex_len: int) -> str:
    """Generate random hex string of exact length."""
    return bytes(randint(0, 255) for _ in range(hex_len // 2)).hex().upper()


def confidential_ciphertext() -> str:
    """Random 132-hex-char ElGamal ciphertext (correct length, garbage bytes)."""
    return _garbage_hex(_CIPHERTEXT_HEX_LEN)


def confidential_commitment() -> str:
    """Random 66-hex-char Pedersen commitment (correct length, garbage bytes)."""
    return _garbage_hex(_COMMITMENT_HEX_LEN)


def confidential_blinding_factor() -> str:
    """Random 64-hex-char blinding factor (correct length, garbage bytes)."""
    return _garbage_hex(_BLINDING_FACTOR_HEX_LEN)


def confidential_schnorr_proof() -> str:
    """Random 128-hex-char Schnorr proof for Convert/Clawback (correct length, garbage)."""
    return _garbage_hex(_SCHNORR_PROOF_HEX_LEN)


def confidential_send_proof() -> str:
    """Random 1892-hex-char proof for ConfidentialMPTSend (correct length, garbage)."""
    return _garbage_hex(_SEND_PROOF_HEX_LEN)


def confidential_convert_back_proof() -> str:
    """Random 1632-hex-char proof for ConfidentialMPTConvertBack (correct length, garbage)."""
    return _garbage_hex(_CONVERT_BACK_PROOF_HEX_LEN)


def confidential_encryption_key() -> str:
    """Random 66-hex-char ElGamal public key (correct length, garbage)."""
    return _garbage_hex(_ENCRYPTION_KEY_HEX_LEN)


def confidential_hex(hex_len: int) -> str:
    """Random hex string of exact length — public wrapper for arbitrary sizes."""
    return _garbage_hex(hex_len)


def confidential_wrong_length_hex(correct_len: int) -> str:
    """Generate hex string with WRONG length (shorter or longer than correct_len)."""
    if random() < 0.5:
        bad_len = max(2, correct_len - randint(2, 20))
    else:
        bad_len = correct_len + randint(2, 20)
    # Ensure even length for valid hex
    bad_len = bad_len if bad_len % 2 == 0 else bad_len + 1
    return _garbage_hex(bad_len)


def confidential_mpt_amount() -> int:
    """Random plaintext MPT amount for confidential transactions."""
    return randint(1, 10_000)


def confidential_zero_hex(hex_len: int) -> str:
    """Correct-length hex string of all zeros — degenerate case for crypto verifiers."""
    return "0" * hex_len


def confidential_negative_amount() -> int:
    """Negative MPT amount — tests integer underflow handling."""
    return -randint(1, 10_000)


def confidential_overflow_amount() -> int:
    """Amount > 2^63 — tests integer overflow in OutstandingAmount."""
    return 2**63 + randint(1, 1_000_000)


def confidential_invalid_flags() -> int:
    """Undefined flag bits — tests flag validation in rippled."""
    return choice([0x80000000, 0xFF000000, 0x40000000, randint(1, 0xFFFFFFFF)])


def confidential_not_on_curve_point() -> str:
    """33-byte blob that looks like a compressed EC point but is invalid.

    Prefix 02/03 followed by 32 random bytes — statistically not on secp256k1.
    """
    prefix = choice(["02", "03"])
    return prefix + _garbage_hex(64)

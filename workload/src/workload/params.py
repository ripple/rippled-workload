"""Centralized random parameter generators; call at point of use, never cache."""

from xrpl.models.transactions import SponsorshipTransferFlag

from workload import confidential_crypto as _cc
from workload.randoms import choice, randint, random


# ── Fuzzing ──────────────────────────────────────────────────────────
def should_send_faulty() -> bool:
    return random() < 0.5


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


def new_account_funding_amount() -> str:
    """Strictly above this network's 10 XRP ReserveBase (genesis_ledger.json) so every
    fund-new delivers enough to CREATE the AccountRoot — an under-delivery is
    tecNO_DST_INSUF_XRP that creates nothing and defeats the state-bloat purpose. A
    fresh bare AccountRoot owns no objects, so only ReserveBase applies (not the 2 XRP
    ReserveIncrement). 11-30 XRP a pop lets a 100k-XRP account fund thousands before draining."""
    return str(randint(11_000_000, 30_000_000))


def fund_new_burst_count() -> int:
    """Accounts to create per PaymentFundNew call, each from a DISTINCT source (same
    source would reuse one Sequence and collide). A burst lands in one open ledger, so
    the state SHAMap gains ~2N nodes at once (N created + N modified by fee debit) —
    concentrated growth widens the SHAMapMissingNode acquisition window more than the
    same creations spread thin. Capped at len(accounts) by the caller."""
    return randint(3, 10)


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


# ── Confidential MPT (XLS-0096) ──────────────────────────────────────
# Faulty bases carry blobs of the exact rippled wire length; the xrpl-py models
# validate the SAME lengths, so one set serves both construction and preflight.
# Wrong length → tem* in preflight (only feeds `seen`, never failure).
# On-ledger (rippled) hex lengths — 2x the byte size.
_CIPHERTEXT_HEX_LEN = _cc.CIPHERTEXT_SIZE * 2  # 132 — ElGamal c1||c2 (66 bytes)
_COMMITMENT_HEX_LEN = _cc.COMMITMENT_SIZE * 2  # 66 — Pedersen commitment (33 bytes)
_BLINDING_FACTOR_HEX_LEN = _cc.BLINDING_FACTOR_SIZE * 2  # 64 (32 bytes)
_ENCRYPTION_KEY_HEX_LEN = _cc.ENCRYPTION_KEY_SIZE * 2  # 66 — compressed ElGamal pubkey
_SCHNORR_PROOF_HEX_LEN = _cc.SCHNORR_PROOF_SIZE * 2  # 128 — Convert ZKProof (64 bytes)
_CLAWBACK_PROOF_HEX_LEN = _cc.CLAWBACK_PROOF_SIZE * 2  # 128 — Clawback ZKProof (64 bytes)
_SEND_PROOF_HEX_LEN = _cc.SEND_PROOF_SIZE * 2  # 1892 — Send ZKProof (946 bytes)
_CONVERT_BACK_PROOF_HEX_LEN = _cc.CONVERT_BACK_PROOF_SIZE * 2  # 1632 (816 bytes)

# Trivial on-curve compressed point (02 || x=1): passes preflight EC-parse checks
# but is cryptographically meaningless → preclaim fails tecBAD_PROOF (a validating
# tec that feeds failure). Mirrors rippled's getTrivialCiphertext fixtures.
_TRIVIAL_POINT_HEX = "02" + "00" * 31 + "01"  # 33 bytes / 66 hex


def _garbage_hex(hex_len: int) -> str:
    return bytes(randint(0, 255) for _ in range(hex_len // 2)).hex().upper()


def _trivial_blob(byte_len: int) -> str:
    # Repeated on-curve point so the blob survives preflight EC-parse; trailing
    # partial chunk zero-padded. Matches rippled's getTrivialSendProofHex layout.
    chunk = bytes.fromhex(_TRIVIAL_POINT_HEX)  # 33 bytes
    out = bytearray()
    while len(out) < byte_len:
        out += chunk
    return bytes(out[:byte_len]).hex().upper()


def confidential_ciphertext() -> str:
    return _trivial_blob(_cc.CIPHERTEXT_SIZE)


def confidential_commitment() -> str:
    return _TRIVIAL_POINT_HEX.upper()


def confidential_blinding_factor() -> str:
    # Not an EC point, not parsed in preflight — garbage of the right length is fine.
    return _garbage_hex(_BLINDING_FACTOR_HEX_LEN)


def confidential_encryption_key() -> str:
    # rippled and the model parse this 33-byte point in preflight, so it must be on-curve.
    return _TRIVIAL_POINT_HEX.upper()


def confidential_schnorr_proof() -> str:
    return _trivial_blob(_cc.SCHNORR_PROOF_SIZE)


def confidential_clawback_proof() -> str:
    return _trivial_blob(_cc.CLAWBACK_PROOF_SIZE)


def confidential_send_proof() -> str:
    return _trivial_blob(_cc.SEND_PROOF_SIZE)


def confidential_convert_back_proof() -> str:
    return _trivial_blob(_cc.CONVERT_BACK_PROOF_SIZE)


def confidential_wrong_length_hex(correct_len: int) -> str:
    # Wrong length → tem* in preflight; coverage vector for the length checks only.
    if random() < 0.5:
        bad_len = max(2, correct_len - randint(2, 20))
    else:
        bad_len = correct_len + randint(2, 20)
    bad_len = bad_len if bad_len % 2 == 0 else bad_len + 1
    return _garbage_hex(bad_len)


def confidential_not_on_curve_point() -> str:
    # Prefix 02/03 + 32 random bytes — statistically off-curve → preflight temMALFORMED.
    return choice(["02", "03"]) + _garbage_hex(64)


def confidential_mpt_amount() -> int:
    return randint(1, 10_000)


def confidential_fee() -> str:
    # rippled charges confidential txns base_fee x10; autofill's base fee draws
    # telINSUF_FEE_P, which never validates and starves sometimes(failure). Fixed
    # 1000 drops = 10x with headroom for load escalation.
    return "1000"


def confidential_invalid_flags() -> int:
    return choice([0x80000000, 0xFF000000, 0x40000000, randint(1, 0xFFFFFFFF)])


# ── Price Oracle (XLS-47) ────────────────────────────────────────────
# provider/asset_class are hex-encoded on the wire (xrpl-py validates via
# bytes.fromhex on the serialized string). xrpl-py caps the *hex string* length
# at 256 (provider) / 16 (asset_class), stricter than rippled's byte caps, so
# generate within the xrpl-py bound to keep valid bases constructible.


def oracle_document_id() -> int:
    """UInt32 identifier, unique per owner account (wide range → rare collision)."""
    return randint(0, 2**32 - 1)


def oracle_provider() -> str:
    length = randint(1, 32)
    return bytes(randint(0, 255) for _ in range(length)).hex()


def oracle_asset_class() -> str:
    """≤8 bytes → ≤16 hex chars (xrpl-py's MAX_ORACLE_SYMBOL_CLASS bound)."""
    length = randint(1, 8)
    return bytes(randint(0, 255) for _ in range(length)).hex()


def oracle_asset_price() -> int:
    return randint(1, 10**15)


def oracle_scale() -> int:
    """0-20 (rippled kMaxPriceScale); above → temMALFORMED."""
    return randint(0, 20)


def oracle_price_data_count() -> int:
    """1-5: each pair stays within one owner reserve (>5 needs two)."""
    return randint(1, 5)


def oracle_base_asset() -> str:
    return currency_code()


def oracle_quote_asset() -> str:
    return currency_code()


def oracle_last_update_time() -> int:
    """Unix time; rippled requires it within ±300s of the ledger close time."""
    import time

    return int(time.time())


def oracle_stale_update_time() -> int:
    """Just past the ripple epoch: clears xrpl-py/rippled's epoch floor but is
    centuries before the ledger close → tecINVALID_UPDATE_TIME."""
    return RIPPLE_EPOCH_OFFSET + 1


# ── Sponsorship (XLS-68) ────────────────────────────────────────────
# xrpl-py ships SponsorFlags as a raw int (no enum) — mirror rippled's bits here.
SPF_SPONSOR_FEE = 0x00000001
SPF_SPONSOR_RESERVE = 0x00000002

TF_SPONSORSHIP_END = int(SponsorshipTransferFlag.TF_SPONSORSHIP_END)
TF_SPONSORSHIP_CREATE = int(SponsorshipTransferFlag.TF_SPONSORSHIP_CREATE)
TF_SPONSORSHIP_REASSIGN = int(SponsorshipTransferFlag.TF_SPONSORSHIP_REASSIGN)


def sponsored_account_amount() -> str:
    """Below the base reserve -- the sponsor covers it, not the sender (the feature's point)."""
    return str(randint(1, 5_000_000))


def sponsorship_fee_amount() -> str:
    """XRP drops funding the Sponsorship's fee bucket (0.1-50 XRP)."""
    return str(randint(100_000, 50_000_000))


def sponsorship_max_fee() -> str | None:
    """Per-tx fee cap; None lets FeeAmount alone bound spend."""
    if random() < 0.3:
        return None
    return str(randint(10, 1000))


def sponsorship_reserve_count() -> int:
    """RemainingOwnerCount: how many object reserves the sponsor still covers."""
    return randint(0, 20)


def sponsor_flags() -> int:
    """Weighted toward fee-only sponsorship, the common case; some cover both."""
    r = random()
    if r < 0.5:
        return SPF_SPONSOR_FEE
    if r < 0.8:
        return SPF_SPONSOR_FEE | SPF_SPONSOR_RESERVE
    return SPF_SPONSOR_RESERVE


def sponsor_reserve_flags() -> int:
    """SponsorshipTransfer Create/Reassign require spfSponsorReserve (rippled
    preflight temINVALID_FLAG otherwise); fee bit is an optional add-on."""
    return SPF_SPONSOR_RESERVE | (SPF_SPONSOR_FEE if random() < 0.3 else 0)


# SponsorshipSet transaction flags (tx.Flags) — distinct bit space from the
# Sponsorship ledger object's own lsfSponsorshipRequireSignFor* flags.
TF_SPONSORSHIP_SET_REQUIRE_SIGN_FOR_FEE = 0x00010000
TF_SPONSORSHIP_CLEAR_REQUIRE_SIGN_FOR_FEE = 0x00020000
TF_SPONSORSHIP_SET_REQUIRE_SIGN_FOR_RESERVE = 0x00040000
TF_SPONSORSHIP_CLEAR_REQUIRE_SIGN_FOR_RESERVE = 0x00080000
TF_SPONSORSHIP_DELETE_OBJECT = 0x00100000


def sponsorship_set_flags() -> int:
    """Random require-sign set/clear combo, one choice per axis so the two
    flags on the same axis never collide (rippled temINVALID_FLAG)."""
    fee_axis = choice(
        [0, TF_SPONSORSHIP_SET_REQUIRE_SIGN_FOR_FEE, TF_SPONSORSHIP_CLEAR_REQUIRE_SIGN_FOR_FEE]
    )
    reserve_axis = choice(
        [
            0,
            TF_SPONSORSHIP_SET_REQUIRE_SIGN_FOR_RESERVE,
            TF_SPONSORSHIP_CLEAR_REQUIRE_SIGN_FOR_RESERVE,
        ]
    )
    return fee_axis | reserve_axis

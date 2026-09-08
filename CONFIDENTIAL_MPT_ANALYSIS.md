# Confidential MPT Workload — Fuzzing Analysis (XLS-0096)

## 1. Overview

Confidential MPT extends XLS-33 (regular MPT) with **encrypted balances and confidential transfers** using EC-ElGamal encryption and zero-knowledge proofs. Individual balances and transfer amounts are hidden from validators/observers while supply invariants (`OutstandingAmount ≤ MaxAmount`) remain publicly enforceable.

## 2. New Transaction Types (5 total)

| Transaction | Purpose | Crypto Required |
|---|---|---|
| **ConfidentialMPTConvert** | Public → confidential (self-conversion, opt-in) | ElGamal encryption, Schnorr PoK (first time), blinding factor |
| **ConfidentialMPTSend** | Confidential transfer between holders | Compact sigma proof (192B) + aggregated Bulletproof (754B) = 946B |
| **ConfidentialMPTMergeInbox** | Merge inbox → spending balance | **None** (proof-free, just account + issuance ID) |
| **ConfidentialMPTConvertBack** | Confidential → public | Compact sigma (128B) + Bulletproof (688B) = 816B, blinding factor |
| **ConfidentialMPTClawback** | Issuer forcibly reclaims funds | Compact sigma proof (64B) |

## 3. Crypto Layer — The Core Challenge

Every previous workload constructs an xrpl-py model and calls `submit_tx()`. Confidential MPT requires **real cryptographic artifacts**:

- **ElGamal keypairs** — Separate ElGamal private/public key per holder (33-byte compressed, secp256k1)
- **ElGamal ciphertexts** — 66-byte blobs encrypting amounts under holder/issuer/auditor keys
- **Blinding factors** — 32-byte random scalars for deterministic ciphertext verification
- **Schnorr PoK** — 64 bytes proving key ownership (on first Convert)
- **Compact sigma proofs** — 192B (Send), 128B (ConvertBack), 64B (Clawback)
- **Bulletproof range proofs** — 754B (Send), 688B (ConvertBack)

The crypto library is **[XRPLF/mpt-crypto](https://github.com/XRPLF/mpt-crypto)** — a C library built on `libsecp256k1` + OpenSSL. **No Python bindings exist.**

## 4. xrpl-py Support Status

The `confidential-mpt` branch of xrpl-py has transaction models for all 5 types. Models accept crypto fields as hex strings — they validate **field lengths** but do no crypto generation. Branch version: `4.6.0b0+confidentialmpt`.

## 5. The 5 Fuzzing Tiers

### Tier 1: Structural Fuzzing (NO crypto needed)

Standard pattern used across all existing workloads (`_vault_create_faulty`, `_did_set_faulty`, etc.).

| Mutation | Rippled code path | Expected result |
|---|---|---|
| `fake_issuance_id` — `params.fake_id()` | Ledger object lookup | `tecNO_ENTRY` |
| `non_holder_submits` — Unauthorized account | Permission check | `tecNO_AUTH` |
| `non_owner_signs` — Account A in tx, B signs | Signature validation | `tefBAD_AUTH` |
| `issuer_as_holder` — Issuer submits holder-only tx | Role check | `tecNO_PERMISSION` |
| `holder_as_issuer` — Non-issuer submits Clawback | Issuer validation | `tecNO_PERMISSION` |
| `invalid_flags` — Undefined flag bits (`0x80000000`) | Flag validation | `temINVALID_FLAG` |
| `self_clawback` — Issuer claws back from self | Self-reference check | Rejection |
| `self_send` — Send to own account | Self-transfer check | Rejection |

### Tier 2: Crypto Blob Fuzzing (NO real crypto needed — HIGH VALUE)

Unique to Confidential MPT. Send blobs that are wrong shape/size/content.

| Mutation | What it tests in rippled | Expected result |
|---|---|---|
| `wrong_length_proof` — Random-length `zk_proof` | Proof length validation | `temMALFORMED` |
| `wrong_length_ciphertext` — Not 66 bytes | Ciphertext format check | `temMALFORMED` |
| `wrong_length_commitment` — Not 33 bytes | Commitment format check | `temMALFORMED` |
| `wrong_length_blinding_factor` — Not 32 bytes | Blinding factor check | `temMALFORMED` |
| `all_zero_proof` — Correct length, all zeros | Crypto verification | `tecPROOF_INVALID` |
| `random_garbage_proof` — Correct length, random hex | Crypto verification | `tecPROOF_INVALID` |
| `truncated_proof` — Valid-length cut in half | Length/parsing check | `temMALFORMED` |
| `swapped_fields` — Ciphertext↔proof | Type confusion | `temMALFORMED` or crash |
| `point_not_on_curve` — 33-byte invalid point | Point decompression | Crash / `tecPROOF_INVALID` |

**Why this is highest-value**: Crypto validation is the newest, most complex code. C/C++ crypto libraries have memory safety concerns. Malformed input handling is where bugs hide.

### Tier 3: Semantic/Business Logic Fuzzing (NO crypto needed)

| Mutation | Edge case tested |
|---|---|
| `mpt_amount = 0` | Zero-amount handling, divide by zero |
| `mpt_amount < 0` | Integer underflow |
| `mpt_amount > 2^63` | Integer overflow in OutstandingAmount |
| `convert_more_than_public_balance` | Over-conversion, phantom tokens |
| `convert_back_more_than_confidential` | Balance underflow in confidential domain |
| `merge_inbox_when_empty` | State corruption on double-merge |
| `clawback_more_than_holder_has` | Negative balance, supply invariant break |
| `send_entire_balance_then_send_again` | Empty balance underflow |

### Tier 4: State Machine / Concurrency Fuzzing (Antithesis sweet spot)

| Scenario | What breaks | Why Antithesis is needed |
|---|---|---|
| Send before MergeInbox | `CB_S_Version` staleness | Requires specific async ordering |
| Concurrent Sends to same receiver | `CB_IN` accumulation race | Race in inbox balance update |
| Convert during pending Send | Public↔confidential boundary | State snapshot inconsistency |
| Clawback during MergeInbox | Atomic operation conflict | Classic TOCTOU bug |
| ConvertBack entire balance then Send | Zero-balance after conversion | Stale balance reference |
| Double MergeInbox | Idempotency of merge | Inbox double-counted |
| Send → Clawback → MergeInbox | Post-clawback inbox validity | Complex state dependency |

### Tier 5: Valid Crypto Path (REQUIRES mpt-crypto bindings)

Needed to reach `doApply()` logic: ciphertext arithmetic, balance updates, `CB_S_Version` increment, auditor ciphertext storage, cross-account invariant checks.

## 6. Coverage Matrix

| Transaction | Tier 1 | Tier 2 | Tier 3 | Tier 4 | Tier 5 |
|---|---|---|---|---|---|
| **ConfidentialMPTConvert** | ✅ | ✅ | ✅ | ✅ | ❌ needs mpt-crypto |
| **ConfidentialMPTSend** | ✅ | ✅ | ✅ | ✅ | ❌ needs mpt-crypto |
| **ConfidentialMPTMergeInbox** | ✅ | N/A | ✅ | ✅ | ✅ NO CRYPTO NEEDED |
| **ConfidentialMPTConvertBack** | ✅ | ✅ | ✅ | ✅ | ❌ needs mpt-crypto |
| **ConfidentialMPTClawback** | ✅ | ✅ | ✅ | ✅ | ❌ needs mpt-crypto |

## 7. MergeInbox — The MVP Entry Point


## 8. What Makes This Different From Every Other Workload

1. **Crypto blob fields are opaque to xrpl-py** — xrpl-py checks lengths but not crypto validity. Rippled checks everything. The gap between client and server validation is where bugs hide.
2. **State is split across two balance domains** — Public + confidential (CB_S + CB_IN). Converting between domains is a state transition no other XRPL feature has. Off-by-one errors = supply invariant violations.
3. **`CB_S_Version` is a concurrency primitive** — Basically an optimistic lock version number. If rippled doesn't correctly reject stale versions = double-spend bugs. Antithesis is PERFECT for finding these.
4. **Clawback breaks the privacy model** — Privileged issuer operation using equality proof (not range proof). Wrong equality proof verification = issuers could steal funds.
5. **Auditor encryption is optional** — When `AuditorEncryptionKey` is present, Send/Convert/ConvertBack must include `auditor_encrypted_amount`. Both paths (with/without auditor) double the state space.

## 9. Setup Phase Dependencies

```
MPTokenIssuanceCreate (tfMPTCanConfidentialAmount)
  → MPTokenIssuanceSet (register IssuerEncryptionKey)
    → MPTokenAuthorize (holders authorize)
      → Payment (public MPT distribution to 2nd account)
        → ConfidentialMPTConvert (2nd account opts-in, converts to CB)
          → ConfidentialMPTMergeInbox (merge CB_IN → CB_S)
```

For each holder: generate ElGamal keypair → ConfidentialMPTConvert (opt-in) → MergeInbox.

## 10. State Model Requirements

New dataclass needed in `models.py`:
```python
@dataclass
class ConfidentialMPTHolder:
    account: str           # holder address
    issuance_id: str       # MPTokenIssuanceID
    has_converted: bool    # opted into confidential
    # Below only tracked when mpt-crypto bindings available:
    elgamal_private_key: str = ""   # hex
    elgamal_public_key: str = ""    # hex
    spending_balance: int = 0       # plaintext mirror of CB_S
    inbox_balance: int = 0          # plaintext mirror of CB_IN
    cb_s_version: int = 0           # tracks CB_S_Version
```

Per-issuance tracking:
```python
@dataclass
class ConfidentialMPTIssuance:
    mpt_issuance_id: str
    issuer: str
    issuer_encryption_key: str       # hex, 33 bytes compressed
    auditor_encryption_key: str = "" # optional
    confidential_outstanding: int = 0
```

## 11. Implementation Plan (Phased)

### Phase 1 — Faulty Workloads (No crypto library needed)
**Owner:** Manasi | **Effort:** ~1 week | **Status:** Starting now — no blockers

Faulty handlers submit transactions with deliberately wrong/garbage data — garbage proofs (correct-length random hex), wrong signers, fake issuance IDs, wrong-length blobs, zero/overflow amounts, invalid encryption keys. The goal is to test that rippled **rejects bad inputs cleanly without crashing**. No real encryption or ZK proofs are needed because we're testing the rejection path, not the success path. MergeInbox is fully implementable here (valid + faulty) since it has no crypto fields at all — just `account` + `mpt_issuance_id`.

**What we're building:**

| Transaction Type | Valid Handler | Faulty Handler |
|---|---|---|
| MergeInbox | ✅ Yes (no crypto needed) | ✅ Yes |
| Convert | ❌ Stub only (needs crypto for success) | ✅ Yes |
| Send | ❌ Stub only (needs crypto for success) | ✅ Yes |
| ConvertBack | ❌ Stub only (needs crypto for success) | ✅ Yes |
| Clawback | ❌ Stub only (needs crypto for success) | ✅ Yes |

**Faulty mutations covered (thorough — not just missing fields, per Ram's feedback):**

| Category | What we send | What rippled should do |
|---|---|---|
| **Missing/Empty fields** | Missing proof field, empty encryption key | Reject — `temMALFORMED` |
| **Wrong length blobs** | Proof too short, too long, truncated | Reject — `temMALFORMED` |
| **Wrong values (correct format)** | Random garbage hex at correct length, wrong encryption keys, swapped proof↔ciphertext fields | Reject — `tecPROOF_INVALID` |
| **Structural** | Fake issuance ID, wrong signer, non-holder, non-issuer, self-send, self-clawback | Reject — various tec/tef codes |
| **Boundary values** | Zero amount, negative amount, overflow (>2^63), convert more than balance | Reject — `temMALFORMED` or `tecINSUFFICIENT_FUNDS` |
| **Invalid crypto blobs** | All-zero proof, point-not-on-curve (33-byte invalid EC point), random ciphertext | Reject — `tecPROOF_INVALID` or crash (bug found!) |
| **Invalid flags** | Undefined flag bits (`0x80000000`) | Reject — `temINVALID_FLAG` |

**Code deliverables:**
- `transactions/confidential_mpt.py` — All 5 handlers (MergeInbox valid + faulty, other 4 faulty only)
- `params.py` — Generators for fake proofs (64B, 816B, 946B), ciphertexts (66B), blinding factors (32B), commitments (33B)
- `models.py` — `ConfidentialMPTHolder` dataclass for per-account state tracking
- `transactions/__init__.py` — 5 new REGISTRY entries
- `test_composer/` — 5 new shell scripts (`parallel_driver_confidential_mpt_*.sh`)
- `scripts/check-imports` — Add new module
- Assertions catalog entries for all 5 types

### Phase 2 — Crypto Integration + Happy Path (Needs mpt-crypto C library)
**Owner:** Vlad (C library integration), Manasi (Python workload handlers) | **Effort:** ~2-3 weeks | **Status:** Blocked on Vlad + Ayaz discussion

Valid handlers submit transactions with **real ElGamal encryption and real ZK proofs** that are mathematically correct. This requires the `mpt-crypto` C library to generate actual Schnorr proofs, sigma proofs, and Bulletproof range proofs from Python. Vlad is owning the library integration — he's talking to Ayaz (who builds the CI/build infra for `mpt-crypto`) about the best approach. He's leaning toward **fetching a prebuilt shared library** rather than building from source, and wants a **generic solution** that's reusable for future features like Smart Escrow.

Once the crypto layer is available, we plug real proof generation into the `_valid` handlers and test that rippled **accepts and processes valid confidential transactions correctly** — encrypted balance updates, CB_S_Version increments, inbox accumulation, auditor ciphertext storage, and supply invariant enforcement.

**What we'll build (once crypto integration is ready):**
- Valid handlers for Convert, Send, ConvertBack, Clawback with real crypto proofs
- Per-account crypto state tracking (ElGamal keypairs, blinding factors, encrypted balances, CB_S_Version)
- Plaintext balance mirrors (spending balance, inbox balance) for assertion validation
- State updaters for `tesSUCCESS` results — update tracked balances when transactions succeed
- `_META_EXPECTATIONS` entries for all 5 types
- Setup phase in `setup.py`: MPTokenIssuanceCreate (with `tfMPTCanConfidentialAmount`) → IssuerEncryptionKey registration → holder authorize → public MPT distribution → ConfidentialMPTConvert (opt-in) → MergeInbox

### Phase 3 — State Machine + Concurrency Fuzzing (Antithesis sweet spot)
**Owner:** Manasi | **Effort:** ~1 week | **Status:** After Phase 2

This phase targets timing-dependent and ordering-dependent bugs that only appear when multiple operations happen concurrently or in specific sequences. This is exactly what Antithesis is designed to find — it explores different thread interleavings and event orderings that are nearly impossible to test manually.

**Concurrency scenarios we'll test:**

| Scenario | What could break | Why it matters |
|---|---|---|
| Send before MergeInbox | `CB_S_Version` staleness → proof generated against stale balance | Could lead to double-spend |
| Concurrent Sends to same receiver | Inbox (`CB_IN`) accumulation race | Receiver balance corruption |
| Clawback during MergeInbox | Atomic operation conflict (TOCTOU) | Clawed-back funds still get merged |
| Convert during pending Send | Public↔confidential boundary race | Balance counted in both domains |
| ConvertBack entire balance then Send | Zero-balance after conversion | Stale balance reference allows spend |
| Double MergeInbox back-to-back | Idempotency check | Inbox double-counted, inflated balance |
| Send → Clawback → MergeInbox | Post-clawback inbox validity | Complex 3-way state dependency |

**Code deliverables:**
- Composite test_composer scripts calling multiple endpoints in rapid succession
- State machine assertions ("after MergeInbox, CB_IN must be EncZero")
- Rapid-fire endpoint combinations for timing-sensitive scenarios

### Parallel Track — Pipeline + Sprint Planning
**Owner:** Manasi | **Timeline:** This week

| Item | Action |
|---|---|
| **Antithesis pipeline → 3.2.0** | Learn how to point pipeline to 3.2.0 branch, coordinate with Vlad |
| **3.2.0 MR** | Rebase and merge once Ram's fix for pre-existing failing test lands |
| **Sprint planning** | Discuss with Sudipto, iron out stories/tickets before next sprint starts |

## 12. Effort Estimate

| Component | Effort | Owner | Notes |
|---|---|---|---|
| Phase 1: Faulty handlers + MergeInbox | **~1 week** | Manasi | No blockers, starting now |
| Phase 2: C library integration | **~1-2 weeks** | Vlad + Ayaz | Generic approach, reusable for Smart Escrow |
| Phase 2: Valid handlers + setup | **~1-2 weeks** | Manasi | After Vlad delivers integration |
| Phase 3: Concurrency fuzzing | **~1 week** | Manasi | After Phase 2 |
| Pipeline setup for 3.2.0 | **~1-2 days** | Manasi | Parallel with Phase 1 |
| **Total** | **~4-6 weeks** | | Phase 1 delivers ~80% of fuzzing value |

**Key insight: Phase 1 (faulty workloads) delivers ~80% of fuzzing value with ~20% of the effort.** The most critical bugs — crashes, buffer overflows, assertion failures in C++ crypto code — are found by throwing malformed inputs at rippled. The happy path (Phase 2) tests correctness but the rejection path tests safety.

## 13. Decisions Made

### From Vlad (Slack, May 20):
- **Vlad will own the C library integration** — talking to Ayaz about build/CI approach
- **Leaning toward fetching prebuilt `.so`** rather than building from source in Docker
- **Wants a generic approach** — reusable for Smart Escrow and future C library integrations
- **Manasi should focus on Phase 1** while crypto integration is being figured out

### From Ram (Standup, May 20):
- **Be thorough with faulty testing** — not just missing fields, also wrong encryption keys, wrong-but-valid-format proofs
- **Get crypto integration done sooner rather than later** — real value is in wrong-but-valid-format inputs
- **Look into pointing Antithesis pipeline to 3.2.0** — coordinate with Vlad
- **Discuss sprint planning with Sudipto** before next sprint starts

## 14. Open Questions (Remaining)

### For Vlad (pending his availability):
1. Should per-account crypto state (ElGamal keypair, blinding factors, encrypted balance, CB_S_Version) go in `UserAccount` or a separate dataclass?
2. Where in the `setup.py` dependency chain should confidential MPT setup go? (Proposed: after `mpt_distribution`)
3. Timeline estimate for the generic C library integration?

### For Ram:
1. Which rippled branch has the Confidential MPT transactors?
2. Specific edge cases in the C++ transactor code the team is worried about?

## 15. Key Takeaway

**Phase 1 is unblocked and starting now.** We submit all 5 Confidential MPT transaction types with every kind of wrong input — garbage proofs, wrong encryption keys, fake IDs, overflow amounts, invalid flags — and verify rippled handles them without crashing. MergeInbox gets full valid+faulty coverage since it needs zero crypto. Phase 2 (valid transactions with real proofs) is owned by Vlad for the C library integration, with Manasi adding the Python handlers once the integration is ready.

# Adding a Workload — Session Cheat Sheet

> One-page glance card for the Antithesis <> Ripple workload deep-dive.
> Tabs to have open: `transactions/escrow.py` · `CLAUDE.md` (Add a transaction type) ·
> `test_composer/all_transactions/parallel_driver_escrow_create_random.sh`

---

## The 30-second framing (say this first)

> "Adding a workload = one HTTP endpoint + a two-line driver script. Antithesis just
> hits URLs in parallel; all the XRPL smarts live behind the URL in our FastAPI server.
> We have ~80 of these, one per transaction type."

Show the whole Antithesis-side integration:

```bash
#!/usr/bin/env bash
curl --silent http://workload:8000/escrow/create/random
```

> "That's the entire file. Antithesis runs it over and over — it knows nothing about XRPL."

---

## The handler shape (what you actually author)

Three parts, **valid + faulty share one `_base()` builder**:

```python
async def escrow_create(accounts, escrows, client):
    if params.should_send_faulty():
        return await _escrow_create_faulty(...)   # one random mutation
    return await _escrow_create_valid(...)         # builds REAL ledger state
```

- **`_valid`** — well-formed tx; on success **tracks the created object** so later
  Finish/Cancel have something real to act on.
- **`_faulty`** — picks ONE mutation via `choice([...])`; MUST include a `"fuzz"`
  choice **plus ≥1 curated tec vector** (fake ID, zero amount, non-owner, overdraw).

---

## "You can't half-add a type" — 5 CI gates (the credibility line)

| Gate | Checks |
|------|--------|
| `check-imports` | every module imports |
| `check-endpoints` | every endpoint registered |
| `check-fuzz-coverage` | every `_faulty` wires generative fuzz (the `"fuzz"` choice) |
| `check-modifier-coverage` | every type classified vs. every modifier (applies / excluded+why) |
| `check-assembler-roundtrip` | raw-fuzz byte-identity holds |

> "Miss a step and CI blocks you, rather than letting a silently-untested type in."

---

## The 10-step checklist (from CLAUDE.md)

1. `params.py` — random generator per field
2. `transactions/<name>.py` — dispatch + `_valid` + `_faulty` + `_base()`
3. `models.py` — dataclass **if** object needs state tracking
4. `transactions/__init__.py` — REGISTRY 5-tuple
5. `parallel_driver_<name>_random.sh` — two-line curl
6. `check-imports` — add import
7. `check-endpoints` — add path
8. `setup.py` — creation logic **if** others depend on the object
9. `modifiers.py` — classify vs. ticket/delegate/sponsor
10. Run the gates

**REGISTRY 5-tuple:** `(name, path, handler_fn, args_fn, state_updater | None)`

---

## Two gotchas that show depth

1. **Preconditions return early silently on empty state** — never raise, never log.
   A non-XRPL exception trips `unreachable()` and fails the whole run.
   (Every `_faulty` starts `if not accounts: return`.)
2. **Setup dependency chain** — can't distribute a token before issuing it; can't test
   permissioned-DEX before credentials accepted. Dependency object → add a setup step
   in the right position (gateways → trust_lines → mpt → vaults → nfts → credentials ...).

**Also:** always submit via `submit_tx()` (wires the event lifecycle + modifiers),
never `xrpl_submit` directly. `submit_raw` = malformations xrpl-py rejects at
construction. `submit_fuzzed` = the `"fuzz"` branch.

---

## Likely follow-ups — short answers

- **"How are faults meaningful?"** Each targets a specific `tec` code; keep ≥1 tec vector
  or the `failure` bucket starves (tem/tef never enter a ledger).
- **"Your fuzzing vs. the fuzzer container?"** Ours attacks the *transaction* path over
  RPC; the `fuzzer` container is a separate binary attacking the *peer-to-peer* layer.
- **"Why a web server?"** So driver scripts stay dumb — smarts live in one place we control.
- **"Test before merge?"** Locally via docker-compose (`LOCAL_TESTING.md`) for "does it
  work" + a short `duration=0.5` dispatch; the 23h nightly is where real findings come from.
- **"Valid txns — why bother?"** They build real state so malformed ones have something
  to act on and invariants have something to check.

---

## Numbers to have handy

- **3 repos** — `xrpld-private` (under test) · `rippled-antithesis` (launch/CI) · `rippled-workload` (everything else)
- **~30** tx modules · **~81** registered types · **~80** driver scripts
- **4 fuzzing bands** — valid → curated faults → generative → raw bytes
- **5 CI gates** · **7 nodes** (1 fault-shielded tracking node + val0–4 + fuzzer)

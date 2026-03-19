# rippled-workload

Transaction traffic generator for XRPL networks. Submits realistic transaction patterns (Payments, OfferCreates, AMM operations, NFTokens, MPTokens, Batch, etc.) driven by ledger closes, and tracks every transaction through to validation.

Built for [Antithesis](https://antithesis.com/) testing of [rippled](https://github.com/XRPLF/rippled).

## Quick Start

### Prerequisites

- [uv](https://docs.astral.sh/uv/getting-started/installation/) (Python package manager)
- Docker + Docker Compose (for running a local rippled network)
- [`generate-ledger`](https://github.com/legleux/generate-ledger) (strongly recommended — pre-bakes accounts, trust lines, and AMM pools into genesis)

### 1. Generate and start a testnet

```bash
cd workload

# Generate a 5-validator testnet with 4 gateways + 100 users
uv run gen auto -o testnet -v 5 -n 104 \
  --gateways 4 \
  --assets-per-gateway 4 \
  --gateway-currencies "USD,CNY,BTC,ETH" \
  --gateway-coverage 1.0 \
  --gateway-connectivity 1.0 \
  --algo secp256k1

# Start it (leave this running)
cd testnet && docker compose up -d && cd ..
```

This exposes rippled on `localhost:5005` (RPC) and `localhost:6006` (WebSocket).

> **Note**: `--gateway-coverage 1.0 --gateway-connectivity 1.0` bakes all trust lines and IOU balances into genesis so the workload starts submitting immediately. `--algo secp256k1` is required — the workload hardcodes SECP256K1 key derivation.

<details>
<summary>Targeting an existing node instead</summary>

Point the workload at any running rippled node via environment variables. It will provision accounts, trust lines, and AMM pools from scratch using the genesis account. This works but takes several minutes on startup.

```bash
RPC_URL="http://mynode:5005" WS_URL="ws://mynode:6006" uv run workload
```

</details>

### 2. Run the workload

```bash
cd workload
uv sync          # first time only
rm -f state.db   # if targeting a fresh network

uv run workload
```

### 3. Verify it's working

| URL | What you'll see |
|-----|----------------|
| http://localhost:8000/state/dashboard | Live dashboard — stats, transaction stream, type controls |
| http://localhost:8000/docs | Swagger UI for all API endpoints |
| https://custom.xrpl.org/localhost:6006 | XRPL Explorer showing your network's ledger progression |

---

## What It Does

On startup, the workload loads state via the fastest available path:

| Path | When | Speed |
|------|------|-------|
| **SQLite resume** | `state.db` exists from a previous run | Instant |
| **Genesis import** | `accounts.json` from `gen auto` | Seconds |
| **Full provisioning** | No prior state, bare network | Minutes |

Then it continuously submits transactions driven by ledger closes, tracking each through validation (WebSocket, primary) or expiry (RPC polling, fallback).

**Sequence safety**: within each batch, one unique account is pre-assigned per concurrent task — no two tasks ever share an account. Across sequential batches an account may accumulate up to `max_pending_per_account` in-flight transactions, with sequence numbers allocated by local incrementing. This eliminates `terPRE_SEQ` / `tefPAST_SEQ` errors by construction.

Transaction types are weighted by config — defaults: Payment 25%, OfferCreate 20%, AMMDeposit 15%, AMMWithdraw 10%, remainder shared evenly across other enabled types.

---

## Throughput Tuning

> [!IMPORTANT]
> **The account pool is the primary throughput ceiling.**
> The workload submits at most `accounts × max_pending_per_account` transactions per ledger.
> If you set *Target txns/ledger* to 500 but only have 100 accounts with `max_pending_per_account = 4`,
> your real ceiling is 400 — the target cap is never reached.
>
> To increase throughput, increase the account count first, then tune the other knobs.

### Knobs, in order of impact

| Knob | Where | Effect |
|------|-------|--------|
| Account count (`-n` in `gen auto`) | `gen auto` + `config.toml [users] number` | **The only lever** — throughput ceiling = account count |
| `max_pending_per_account` | `config.toml [transactions]` | Must stay at 1 — raising it causes `tefPAST_SEQ` (see config comment) |
| *Target txns/ledger* (UI / `POST /workload/target-txns`) | Runtime | Hard cap on batch size per cycle |

### Example: targeting 500 txns/ledger

```bash
# Generate with 200 users (200 accounts × max_pending=4 = 800 ceiling, well above 500)
uv run gen auto -o testnet -v 5 -n 204 \
  --gateways 4 --assets-per-gateway 4 \
  --gateway-currencies "USD,CNY,BTC,ETH" \
  --gateway-coverage 1.0 --gateway-connectivity 1.0 \
  --algo secp256k1
```

Update `config.toml` to match:
```toml
[users]
number = 200

[genesis]
user_count = 200
```

Then in the dashboard set *Target txns/ledger* to 500.

---

## Project Structure

```
rippled-workload/
├── workload/                   # Main application
│   └── src/workload/
│       ├── app.py              # FastAPI app, endpoints, dashboard, submission loop
│       ├── workload_core.py    # Workload class, sequence management, state tracking
│       ├── txn_factory/
│       │   └── builder.py      # Transaction builders (registry pattern)
│       ├── ws.py               # WebSocket listener
│       ├── ws_processor.py     # WS event dispatcher (validation, ledger close)
│       ├── sqlite_store.py     # Persistent state (wallets, currencies, tx history)
│       ├── validation.py       # ValidationRecord, ValidationSrc data types
│       ├── amm.py              # AMM pool registry and DEX metrics
│       ├── balances.py         # In-memory balance tracker
│       ├── constants.py        # TxType, TxState, TERMINAL_STATE, PENDING_STATES
│       ├── fee_info.py         # Fee escalation data
│       ├── randoms.py          # SystemRandom (Antithesis deterministic replay)
│       └── config.toml         # Accounts, currencies, tx weights, timeouts
├── sidecar/                    # Antithesis monitoring sidecar (separate container)
├── test_composer/              # Curl-based load test scripts
├── scripts/                    # Standalone utilities (ledger monitor, health check)
└── specs/                      # Feature specs
```

---

## Configuration

Key settings in `workload/src/workload/config.toml`:

| Section | Key settings |
|---------|-------------|
| `[gateways]` | `number = 4` — must match `--gateways` in `gen auto` |
| `[users]` | `number = 100` — must match user count in `gen auto` |
| `[genesis]` | `accounts_json`, `gateway_count`, `user_count`, `currencies` — must align with `gen auto` flags |
| `[transactions]` | `max_pending_per_account = 1` (must not be raised — see Throughput Tuning), `percentages`, `disabled` |
| `[rippled]` | Connection settings (auto-detected from env vars) |

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `RPC_URL` | `http://localhost:5005` | RPC endpoint |
| `WS_URL` | `ws://localhost:6006` | WebSocket endpoint |

---

## Documentation

- **[workload/README.md](workload/README.md)** — Full API reference, configuration, architecture
- **[workload/ws-architecture.md](workload/ws-architecture.md)** — WebSocket validation architecture
- **[workload/docs/todo/TODO.md](workload/docs/todo/TODO.md)** — Prioritized roadmap
- **[specs/001-priority-improvements/spec.md](specs/001-priority-improvements/spec.md)** — Active feature spec

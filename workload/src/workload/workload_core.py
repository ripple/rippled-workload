import asyncio
import hashlib
import logging
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from typing import Any

import xrpl
from xrpl.asyncio.clients import AsyncJsonRpcClient
from xrpl.asyncio.ledger import get_latest_validated_ledger_sequence
from xrpl.core.binarycodec import encode, encode_for_signing
from xrpl.core.keypairs import sign
from xrpl.models import IssuedCurrency, SubmitOnly, Transaction
from xrpl.models.amounts import IssuedCurrencyAmount
from xrpl.models.requests import (
    AccountInfo,
    ServerState,
    Tx,
)
from xrpl.models.transactions import (
    AccountSet,
    AccountSetAsfFlag,
    AMMCreate,
    Payment,
    TrustSet,
)
from xrpl.wallet import Wallet

import workload.constants as C
from workload.amm import AMMPoolRegistry, DEXMetrics
from workload.assertions import tx_rejected, tx_submitted, tx_validated
from workload.balances import BalanceTracker
from workload.sqlite_store import SQLiteStore
from workload.txn_factory.builder import TxnContext, generate_txn
from workload.validation import ValidationRecord, ValidationSrc

log = logging.getLogger("workload")


@dataclass(slots=True)
class PendingTx:
    tx_hash: str
    signed_blob_hex: str
    account: str
    tx_json: dict
    sequence: int | None
    last_ledger_seq: int
    transaction_type: C.TxType | None
    created_ledger: int
    wallet: Wallet | None = None
    state: C.TxState = C.TxState.CREATED
    attempts: int = 0
    engine_result_first: str | None = None
    engine_result_message: str | None = None
    validated_ledger: int | None = None
    meta_txn_result: str | None = None
    created_at: float = field(default_factory=time.time)
    finalized_at: float | None = None
    account_generation: int = 0  # AccountRecord.generation at build time; used to detect stale pre-signed txns

    def __str__(self):
        return f"{self.transaction_type} -- {self.account} -- {self.state}"


class InMemoryStore:
    """Current snapshot of transaction states and validation history for metrics."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._records: dict[str, dict] = {}
        self.validations: deque[ValidationRecord] = deque(maxlen=5000)
        self.count_by_state: dict[str, int] = {}
        self.count_by_type: dict[str, int] = {}
        self.validated_by_source: dict[str, int] = {}

    def _recount(self) -> None:
        """Full recount — called only on startup or after bulk operations."""
        self.count_by_state = Counter(rec.get("state", "UNKNOWN") for rec in self._records.values())
        self.count_by_type = Counter(rec.get("transaction_type", "UNKNOWN") for rec in self._records.values())
        self.validated_by_source = Counter(v.src for v in self.validations)

    def _update_counts(self, prev_state: str | None, new_state: str | None, txn_type: str | None = None) -> None:
        """Incremental count update — O(1) instead of O(n)."""
        if prev_state:
            self.count_by_state[prev_state] = max(0, self.count_by_state.get(prev_state, 0) - 1)
        if new_state:
            self.count_by_state[new_state] = self.count_by_state.get(new_state, 0) + 1
        if txn_type and not prev_state:  # only count type on first insert
            self.count_by_type[txn_type] = self.count_by_type.get(txn_type, 0) + 1

    async def get(self, tx_hash: str) -> dict | None:
        async with self._lock:
            return self._records.get(tx_hash)

    async def mark(self, tx_hash: str, *, source: str | None = None, **fields) -> None:
        """
        Update or insert a transaction record.

        tx_hash:
            Unique transaction identifier.
        source:
            Origin of the update ("ws", "poll", etc.).
        **fields:
            Additional fields to merge (e.g., state, validated_ledger, meta_txn_result).

        Behavior:
        - Creates/updates the flat record under tx_hash.
        - On first transition to a final state (VALIDATED/REJECTED/EXPIRED), stamps 'finalized_at'.
        - When state == VALIDATED, appends a ValidationRecord(txn, ledger, src) exactly once per (txn, ledger).
        - Recomputes per-state and per-source counters.
        """
        async with self._lock:
            rec = self._records.get(tx_hash, {})
            prev_state = rec.get("state")

            rec.update(fields)
            if source is not None:
                rec["source"] = source

            state = rec.get("state")
            if isinstance(state, C.TxState):
                state = state.name
                rec["state"] = state

            if state in C.TERMINAL_STATE:
                rec.setdefault("finalized_at", time.time())

                if state == "VALIDATED" and prev_state != "VALIDATED":
                    seq = rec.get("validated_ledger") or 0
                    src = source or rec.get("source", "unknown")
                    if not any(v.txn == tx_hash and v.seq == seq for v in self.validations):
                        self.validations.append(ValidationRecord(txn=tx_hash, seq=seq, src=src))
                        self.validated_by_source[src] = self.validated_by_source.get(src, 0) + 1

            self._records[tx_hash] = rec
            self._update_counts(prev_state, state, rec.get("transaction_type"))

    async def rekey(self, old_hash: str, new_hash: str) -> None:
        """
        Replace a record's key when a tx's canonical hash changes.
        - Moves the flat record from old_hash -> new_hash.
        - Updates inner 'tx_hash' field if present.
        - No-op if old_hash doesn't exist.
        """
        async with self._lock:
            rec = self._records.pop(old_hash, None)
            if rec is None:
                return
            rec["tx_hash"] = new_hash
            self._records[new_hash] = rec

    async def find_by_state(self, *states: C.TxState | str) -> list[dict]:
        """Return flat records whose 'state' matches any of the given states."""
        wanted = {s.name if isinstance(s, C.TxState) else s for s in states}
        async with self._lock:
            return [rec for rec in self._records.values() if rec.get("state") in wanted]

    async def all_records(self) -> list[dict]:
        async with self._lock:
            return list(self._records.values())

    def snapshot_stats(self) -> dict:
        return {
            "by_state": dict(self.count_by_state),
            "validated_by_source": dict(self.validated_by_source),
            "total_tracked": len(self._records),
            "recent_validations": len(self.validations),
        }


@dataclass
class AccountRecord:
    lock: asyncio.Lock
    next_seq: int | None = None
    generation: int = 0  # incremented on every cascade-expire / sequence reset


def _sha512half(b: bytes) -> bytes:
    return hashlib.sha512(b).digest()[:32]


def _txid_from_signed_blob_hex(signed_blob_hex: str) -> str:
    return _sha512half(bytes.fromhex("54584E00") + bytes.fromhex(signed_blob_hex)).hex().upper()


def issue_currencies(issuer: str, currency_code: list[str]) -> list[IssuedCurrency]:
    issued_currencies = [IssuedCurrency.from_dict(dict(issuer=issuer, currency=cc)) for cc in currency_code]
    return issued_currencies


class Workload:
    def __init__(self, config: dict, client: AsyncJsonRpcClient, *, store: SQLiteStore | None = None):
        self.config = config
        self.client = client

        self.funding_wallet = Wallet.from_seed(
            "snoPBrXtMeMyMHUVTgbuqAfg1SUTb", algorithm=xrpl.CryptoAlgorithm.SECP256K1
        )

        self.accounts: dict[str, AccountRecord] = {}
        self.wallets: dict[str, Wallet] = {}
        self.gateways: list[Wallet] = []
        self.users: list[Wallet] = []
        self.gateway_names: dict[str, str] = {}  # address -> name mapping

        self.pending: dict[str, PendingTx] = {}

        self.store: InMemoryStore = InMemoryStore()  # Hot-path runtime store
        self.persistent_store: SQLiteStore | None = store  # SQLite for durability (flushed periodically)

        self.max_pending_per_account: int = self.config.get("transactions", {}).get("max_pending_per_account", 1)

        self.target_txns_per_ledger: int = 100

        self._config_disabled_types: frozenset[str] = frozenset(self.config.get("transactions", {}).get("disabled", []))
        self.disabled_txn_types: set[str] = set(self._config_disabled_types)

        self.user_token_status: dict[str, set[tuple[str, str]]] = {}

        self._currencies: list[IssuedCurrency] = []

        self._mptoken_issuance_ids: list[str] = []
        self._credentials: list[dict] = []  # [{issuer, subject, credential_type, accepted}]
        self._vaults: list[dict] = []  # [{vault_id, owner, asset}]
        self._domains: list[dict] = []  # [{domain_id, owner}]

        self.amm = AMMPoolRegistry()
        self.dex_metrics = DEXMetrics()

        self.balance_tracker = BalanceTracker()

        # Workload lifecycle state (set by app.py orchestrator)
        self.workload_started: bool = False
        self.started_at: float = time.time()  # Wall-clock start time

        # Server status — updated by ws_processor via update_server_status()
        self.latest_server_status: dict = {}
        self.latest_server_status_time: float = 0.0
        self.latest_server_status_computed: dict = {}

        # Ledger close notification — set by ws_processor, awaited by consumer
        self._ledger_closed_event: asyncio.Event = asyncio.Event()
        self.latest_ledger_index: int = 0
        self.first_ledger_index: int = 0  # Set on first ledger close event

        # Cached fee — updated from WS ledgerClosed events, invalidated on telINSUF_FEE_P
        self._cached_fee: int | None = None
        # Reserve info from WS ledgerClosed events
        self._ws_reserve_base: int | None = None
        self._ws_reserve_inc: int | None = None
        # Last closed ledger's transaction count (from WS ledgerClosed)
        self.last_closed_ledger_txn_count: int = 0

        # Cumulative counters (never reset, survive cleanup_terminal)
        self._type_submitted: dict[str, int] = {}
        self._type_validated: dict[str, int] = {}
        self._total_created: int = 0
        self._total_validated: int = 0
        self._total_rejected: int = 0
        self._total_expired: int = 0

        self.ctx = self.configure_txn_context(
            wallets=self.wallets,
            funding_wallet=self.funding_wallet,
            config=self.config,
        )

    def configure_txn_context(
        self,
        *,
        funding_wallet: "Wallet",
        wallets: dict[str, "Wallet"] | list["Wallet"],
        currencies: list["IssuedCurrency"] | None = None,
        config: dict | None = None,
    ) -> TxnContext:
        currs = currencies if currencies is not None else self._currencies
        wl = list(wallets.values()) if isinstance(wallets, dict) else list(wallets)
        ctx = TxnContext.build(
            funding_wallet=funding_wallet,
            wallets=wl,
            currencies=currs,
            config=config or self.config,
            base_fee_drops=self._open_ledger_fee,
            next_sequence=self.alloc_seq,
        )
        ctx.mptoken_issuance_ids = self._mptoken_issuance_ids
        ctx.balances = self.balance_tracker.data
        ctx.amm_pools = self.amm.pool_ids
        ctx.amm_pool_registry = self.amm.pools
        ctx.disabled_types = self.disabled_txn_types
        ctx.credentials = self._credentials
        ctx.vaults = self._vaults
        ctx.domains = self._domains
        return ctx

    def update_txn_context(self):
        self.ctx = self.configure_txn_context(
            wallets=list(self.wallets.values()),
            funding_wallet=self.funding_wallet,
        )

    def _register_amm_pool(self, asset1: dict, asset2: dict, creator: str) -> None:
        """Register a new AMM pool after successful AMMCreate validation."""
        self.amm.register(asset1, asset2, creator)
        self.dex_metrics.pools_created = len(self.amm)
        self.update_txn_context()
        log.debug("Registered AMM pool: total %d", len(self.amm))

    def get_all_account_addresses(self) -> list[str]:
        """Return all account addresses we're tracking (for WebSocket subscription).

        Returns empty list if no accounts initialized yet (WS will fall back to transaction stream).
        """
        if not self.wallets:
            return []

        addresses = [self.funding_wallet.address]
        addresses.extend(self.wallets.keys())
        return addresses

    def get_account_display_name(self, address: str) -> str:
        """Get display name for an account (gateway name if available, else address)."""
        return self.gateway_names.get(address, address)

    def load_state_from_store(self) -> bool:
        """Load workload state from SQLite store if available.

        On hot-reload, this allows us to skip re-creating accounts and TrustSets.
        We clear any pending transactions (stale from previous session) and
        reset sequence numbers from on-chain state.

        Returns:
            True if state was loaded, False otherwise
        """
        from workload.sqlite_store import SQLiteStore

        if not isinstance(self.persistent_store, SQLiteStore):
            log.warning("Store is not SQLiteStore, cannot load state")
            return False

        if not self.persistent_store.has_state():
            log.debug("No persisted state found in database")
            return False

        log.debug("Loading workload state from database...")

        self.pending.clear()

        gateway_names_from_config = self.config.get("gateways", {}).get("names", [])
        wallet_data = self.persistent_store.load_wallets()
        gateway_idx = 0
        for address, (wallet, is_gateway, is_user) in wallet_data.items():
            self.wallets[address] = wallet
            self._record_for(address)

            if is_gateway:
                self.gateways.append(wallet)
                if gateway_idx < len(gateway_names_from_config):
                    self.gateway_names[address] = gateway_names_from_config[gateway_idx]
                    gateway_idx += 1
            if is_user:
                self.users.append(wallet)

        currencies = self.persistent_store.load_currencies()
        self._currencies = currencies

        log.debug(
            f"Loaded state: {len(self.wallets)} wallets "
            f"({len(self.gateways)} gateways, {len(self.users)} users), "
            f"{len(self._currencies)} currencies"
        )

        if len(self.gateways) > 0 and len(self._currencies) == 0:
            log.warning("Incomplete state detected: gateways exist but no currencies found. Rejecting loaded state.")
            self.wallets.clear()
            self.gateways.clear()
            self.users.clear()
            self.gateway_names.clear()
            self._currencies = []
            return False

        self.update_txn_context()

        return True

    async def load_from_genesis(self, accounts_json_path: str) -> bool:
        """Load accounts from a pre-generated accounts.json (from generate_ledger).

        Skips all init phases — accounts, trust lines, tokens, and AMMs are already
        baked into the genesis ledger.

        Args:
            accounts_json_path: Path to accounts.json with [address, seed] pairs

        Returns:
            True if loaded successfully
        """
        import json as _json
        from pathlib import Path

        path = Path(accounts_json_path)
        if not path.exists():
            log.debug("Genesis accounts file not found: %s", path)
            return False

        with open(path) as f:
            account_data = _json.load(f)

        gateway_count = self.config["gateways"]["number"]
        genesis_cfg = self.config.get("genesis", {})
        currency_codes = genesis_cfg.get("currencies", ["USD", "CNY", "BTC", "ETH"])
        gateway_names = self.config.get("gateways", {}).get("names", [])
        # user_count derived from accounts.json — not config.toml
        user_count = len(account_data) - gateway_count

        log.info(
            "Loading from genesis: %d accounts (%d gateways, %d users)", len(account_data), gateway_count, user_count
        )

        # Build wallets from seeds — try ed25519 first, fall back to secp256k1
        for i, (address, seed) in enumerate(account_data):
            w = Wallet.from_seed(seed, algorithm=xrpl.CryptoAlgorithm.ED25519)
            if w.address != address:
                w = Wallet.from_seed(seed, algorithm=xrpl.CryptoAlgorithm.SECP256K1)
            if w.address != address:
                log.error("Address mismatch for account %d: expected %s, got %s", i, address, w.address)
                continue
            self.wallets[w.address] = w
            self._record_for(w.address)

            if i < gateway_count:
                self.gateways.append(w)
                if i < len(gateway_names):
                    self.gateway_names[w.address] = gateway_names[i]
                self.save_wallet_to_store(w, is_gateway=True)
            else:
                self.users.append(w)
                self.save_wallet_to_store(w, is_user=True)

        # Build currencies (4 per gateway)
        for gw in self.gateways:
            for code in currency_codes:
                self._currencies.append(IssuedCurrency(currency=code, issuer=gw.address))

        self.save_currencies_to_store()
        self.update_txn_context()

        log.info(
            "Genesis loaded: %d gateways, %d users, %d currencies",
            len(self.gateways),
            len(self.users),
            len(self._currencies),
        )

        # Discover AMM pools from the ledger
        await self._discover_amm_pools()

        return True

    async def _discover_amm_pools(self) -> None:
        """Discover existing AMM pools by querying amm_info for known currency pairs."""
        from xrpl.models.currencies import XRP as XRPCurrency
        from xrpl.models.requests import AMMInfo

        discovered = 0
        # Check XRP/IOU pairs (gateway pools)
        for currency in self._currencies:
            try:
                a1 = XRPCurrency()
                a2 = IssuedCurrency(currency=currency.currency, issuer=currency.issuer)
                resp = await self._rpc(AMMInfo(asset=a1, asset2=a2), t=3.0)
                if resp.is_successful() and "amm" in resp.result:
                    self._register_amm_pool(
                        {"currency": "XRP"},
                        {"currency": currency.currency, "issuer": currency.issuer},
                        resp.result["amm"].get("account", "unknown"),
                    )
                    discovered += 1
            except Exception as e:
                log.debug("AMM discovery failed (XRP/IOU pair): %s", e)

        # Check IOU/IOU pairs
        from itertools import combinations

        for c1, c2 in combinations(self._currencies, 2):
            try:
                a1 = IssuedCurrency(currency=c1.currency, issuer=c1.issuer)
                a2 = IssuedCurrency(currency=c2.currency, issuer=c2.issuer)
                resp = await self._rpc(AMMInfo(asset=a1, asset2=a2), t=2.0)
                if resp.is_successful() and "amm" in resp.result:
                    self._register_amm_pool(
                        {"currency": c1.currency, "issuer": c1.issuer},
                        {"currency": c2.currency, "issuer": c2.issuer},
                        resp.result["amm"].get("account", "unknown"),
                    )
                    discovered += 1
            except Exception as e:
                log.debug("AMM discovery failed (IOU/IOU pair %s/%s): %s", c1.currency, c2.currency, e)

        log.info("Discovered %d AMM pools from ledger", discovered)

    def save_wallet_to_store(
        self, wallet: Wallet, is_gateway: bool = False, is_user: bool = False, funded_ledger_index: int | None = None
    ) -> None:
        """Save a wallet to the persistent store."""
        from workload.sqlite_store import SQLiteStore

        if isinstance(self.persistent_store, SQLiteStore):
            self.persistent_store.save_wallet(
                wallet, is_gateway=is_gateway, is_user=is_user, funded_ledger_index=funded_ledger_index
            )

    def save_currencies_to_store(self) -> None:
        """Save all currencies to the persistent store."""
        from workload.sqlite_store import SQLiteStore

        if isinstance(self.persistent_store, SQLiteStore):
            for currency in self._currencies:
                self.persistent_store.save_currency(currency)

    async def _latest_validated_ledger(self) -> int:
        return await get_latest_validated_ledger_sequence(client=self.client)

    def _record_for(self, addr: str) -> AccountRecord:
        rec = self.accounts.get(addr)
        if rec is None:
            log.debug("_record for %s", addr)
            rec = AccountRecord(lock=asyncio.Lock(), next_seq=None)
            self.accounts[addr] = rec
        return rec

    def _set_balance(self, account: str, currency: str, value: float, issuer: str | None = None) -> None:
        self.balance_tracker.set(account, currency, value, issuer)

    def _update_balance(self, account: str, currency: str, delta: float, issuer: str | None = None) -> None:
        self.balance_tracker.update(account, currency, delta, issuer)

    async def _rpc(self, req, *, t=C.RPC_TIMEOUT):
        return await asyncio.wait_for(self.client.request(req), timeout=t)

    async def fetch_ledger_tx_count(self, ledger_index: int) -> int | None:
        """Fetch the number of transactions in a validated ledger. Returns None on failure."""
        from xrpl.models.requests import Ledger as LedgerReq

        resp = await self._rpc(LedgerReq(ledger_index=ledger_index, transactions=True, expand=False))
        if resp.is_successful():
            return len(resp.result.get("ledger", {}).get("transactions", []))
        log.warning("Failed to fetch ledger %s: %s", ledger_index, resp.result)
        return None

    def notify_ledger_closed(self, ledger_index: int) -> None:
        """Called by ws_processor when a ledger closes. Wakes the consumer."""
        self.latest_ledger_index = ledger_index
        self._ledger_closed_event.set()

    async def wait_for_ledger_close(self, timeout: float = 10.0) -> int:
        """Wait for the next ledger close event. Returns the new ledger index."""
        self._ledger_closed_event.clear()
        try:
            await asyncio.wait_for(self._ledger_closed_event.wait(), timeout=timeout)
        except TimeoutError:
            pass
        return self.latest_ledger_index

    def update_server_status(self, msg: dict) -> None:
        """Update server status state from a serverStatus WS message."""
        import time

        self.latest_server_status = msg
        self.latest_server_status_time = time.time()

        load_factor = msg.get("load_factor", 256)
        load_factor_fee_escalation = msg.get("load_factor_fee_escalation")
        load_factor_fee_queue = msg.get("load_factor_fee_queue")
        load_factor_fee_reference = msg.get("load_factor_fee_reference", 256)
        server_status = msg.get("server_status", "unknown")

        queue_multiplier = load_factor_fee_queue / load_factor_fee_reference if load_factor_fee_queue else 1.0
        escalation_multiplier = (
            load_factor_fee_escalation / load_factor_fee_reference if load_factor_fee_escalation else 1.0
        )
        self.latest_server_status_computed = {
            "server_status": server_status,
            "queue_fee_multiplier": queue_multiplier,
            "open_ledger_fee_multiplier": escalation_multiplier,
            "general_load_multiplier": load_factor / 256.0,
        }

    async def alloc_seq(self, addr: str) -> int:
        rec = self._record_for(addr)

        async with rec.lock:
            if rec.next_seq is None:
                ai = await self._rpc(AccountInfo(account=addr, ledger_index="validated", strict=True))
                acct = ai.result.get("account_data")
                if acct is None:
                    raise RuntimeError(f"Account {addr} not found on ledger (unfunded?)")
                rec.next_seq = acct["Sequence"]
                log.debug("First seq for %s: fetched %d from ledger", addr, rec.next_seq)

            s = rec.next_seq
            rec.next_seq += 1
            return s

    async def release_seq(self, addr: str, seq: int) -> None:
        """Release an allocated sequence number back to the pool.

        Used when a transaction gets a tel* (local) error and never submits to the network.
        Only releases if this was the most recently allocated sequence to avoid gaps.
        """
        rec = self._record_for(addr)
        async with rec.lock:
            if rec.next_seq == seq + 1:
                rec.next_seq = seq
                log.debug(f"Released sequence {seq} for {addr} (local error, never submitted)")
            else:
                log.debug(
                    f"Cannot release sequence {seq} for {addr} - next_seq is {rec.next_seq} (gap would be created)"
                )

    async def _open_ledger_fee(self) -> int:
        """Get the fee required to submit a transaction.

        Uses the fee command to get minimum_fee (queue entry) and open_ledger_fee (immediate).
        Caps at MAX_FEE_DROPS to prevent account drainage during extreme escalation.

        Returns:
            Fee in drops. Returns base_fee (10) if queue is empty, minimum_fee if queue has room,
            or raises ValueError if fees exceed MAX_FEE_DROPS.
        """
        MAX_FEE_DROPS = 1000  # Cap to prevent draining accounts (base is 10, this is 100x)

        fee_info = await self.get_fee_info()
        minimum_fee = fee_info.minimum_fee  # Fee to get into queue
        open_ledger_fee = fee_info.open_ledger_fee  # Fee to skip queue and get into open ledger immediately
        base_fee = fee_info.base_fee

        fee = minimum_fee

        log.debug(
            f"Fee: {fee} drops (min={minimum_fee}, open={open_ledger_fee}, base={base_fee}, "
            f"queue={fee_info.current_queue_size}/{fee_info.max_queue_size}, "
            f"ledger={fee_info.current_ledger_size}/{fee_info.expected_ledger_size})"
        )

        if fee > base_fee:
            log.debug(
                f"Queue fees escalated: minimum={minimum_fee} (queue), open_ledger={open_ledger_fee} (immediate), base={base_fee}"
            )

        if fee > MAX_FEE_DROPS:
            raise ValueError(
                f"Fee too high ({fee} drops > {MAX_FEE_DROPS} max) - queue is full, refusing to drain accounts. "
                f"Wait for queue to clear or increase MAX_FEE_DROPS."
            )

        return fee

    async def _last_ledger_sequence_offset(self, off: int) -> int:
        ss = await self._rpc(ServerState(), t=2.0)
        return ss.result["state"]["validated_ledger"]["seq"] + off

    async def _current_ledger_index(self) -> int:
        """Get the latest validated ledger index."""
        ss = await self._rpc(ServerState(), t=2.0)
        return ss.result["state"]["validated_ledger"]["seq"]

    async def server_info(self) -> dict:
        """Get current server info from rippled. Result structure changes, see XRPL docs."""
        from xrpl.models.requests import ServerInfo

        r = await self.client.request(ServerInfo())
        return r.result

    async def get_fee_info(self) -> "FeeInfo":
        """Get current fee escalation state from rippled using the fee command.

        Returns FeeInfo with:
        - expected_ledger_size: Dynamic limit for base fee transactions
        - current_ledger_size: Number of transactions in open ledger
        - current_queue_size: Number of transactions in queue
        - max_queue_size: Maximum queue capacity
        - base_fee, median_fee, minimum_fee, open_ledger_fee: Fee levels in drops

        Note: current_ledger_size and current_queue_size change rapidly (per transaction).
        Call this method fresh when you need current values, don't cache.

        See reference/FeeEscalation.md for detailed documentation.
        """
        from xrpl.models.requests import Fee

        from workload.fee_info import FeeInfo

        r = await self.client.request(Fee())
        return FeeInfo.from_fee_result(r.result)

    async def _expected_ledger_size(self) -> int:
        """Get the expected number of transactions per ledger from the server.

        Uses the fee command which returns expected_ledger_size as part of fee escalation state.
        Raises RuntimeError if expected_ledger_size is not available.
        We should never submit transactions if we can't determine capacity.
        """
        fee_info = await self.get_fee_info()
        return fee_info.expected_ledger_size

    async def record_created(self, p: PendingTx) -> None:
        self.pending[p.tx_hash] = p
        p.state = C.TxState.CREATED
        self._total_created += 1
        if p.transaction_type:
            tt = p.transaction_type
            self._type_submitted[tt] = self._type_submitted.get(tt, 0) + 1
        # Don't persist CREATED to SQLite — it's transient (immediately becomes SUBMITTED).
        # The store write happens in record_submitted() when the txn is on the wire.
        # This avoids a SQLite lock acquisition per transaction during batch building.

    async def record_submitted(self, p: PendingTx, engine_result: str | None, srv_txid: str | None):
        if p.state in C.TERMINAL_STATE:
            pass  # Don't overwrite terminal states. This should probably be an exception.
            return
        old = p.tx_hash
        new_hash = srv_txid or old
        if srv_txid and srv_txid != old:
            self.pending[new_hash] = self.pending.pop(old, p)
            p.tx_hash = new_hash
            await self.store.rekey(old, new_hash)
        p.state = C.TxState.SUBMITTED
        p.engine_result_first = p.engine_result_first or engine_result
        self.pending[new_hash] = p
        if p.transaction_type:
            tx_submitted(p.transaction_type, details={"hash": new_hash, "account": p.account, "sequence": p.sequence})
        await self.store.mark(
            new_hash,
            state=C.TxState.SUBMITTED,
            account=p.account,
            sequence=p.sequence,
            transaction_type=p.transaction_type,
            engine_result_first=p.engine_result_first,
        )

    async def record_validated(self, rec: ValidationRecord, meta_result: str | None = None) -> dict:
        # Check if this is a NEW validation (not a duplicate from WS + poll)
        p_before = self.pending.get(rec.txn)
        was_already_validated = p_before is not None and p_before.state == C.TxState.VALIDATED
        p_live = await self._apply_validation_state(rec, meta_result)
        if p_live and p_live.transaction_type and not was_already_validated:
            tt = p_live.transaction_type
            tx_validated(tt, meta_result or "unknown", details={
                "hash": rec.txn, "ledger_index": rec.seq, "account": p_live.account, "source": rec.src,
            })
            self._type_validated[tt] = self._type_validated.get(tt, 0) + 1
            self._total_validated += 1
        self._on_account_adopted(p_live, rec)
        self._on_payment_validated(p_live, meta_result)
        await self._on_mptoken_created(p_live, rec)
        await self._on_batch_validated(p_live)
        self._on_amm_created(p_live, meta_result)
        self._on_dex_activity(p_live, meta_result)
        self._on_credential_created(p_live)
        self._on_credential_accepted(p_live)
        self._on_credential_deleted(p_live)
        await self._on_vault_created(p_live, rec)
        self._on_vault_deleted(p_live)
        await self._on_domain_created(p_live, rec)
        self._on_domain_deleted(p_live)
        log.debug("txn %s validated at ledger %s via %s", rec.txn, rec.seq, rec.src)
        return {"tx_hash": rec.txn, "ledger_index": rec.seq, "source": rec.src, "meta_result": meta_result}

    async def _apply_validation_state(self, rec: ValidationRecord, meta_result: str | None) -> PendingTx | None:
        """Update pending tx state and persist to store. Returns the live PendingTx or None."""
        p_live = self.pending.get(rec.txn)
        if p_live:
            p_live.state = C.TxState.VALIDATED
            p_live.validated_ledger = rec.seq
            p_live.meta_txn_result = meta_result
        else:
            log.debug("record_validated: tx not in pending (race or already finalized): %s", rec.txn)
        await self.store.mark(
            rec.txn,
            state=C.TxState.VALIDATED,
            validated_ledger=rec.seq,
            meta_txn_result=meta_result,
            source=rec.src,
        )
        return self.pending.get(rec.txn)  # re-fetch after await in case pending changed

    def _on_account_adopted(self, p_live: PendingTx | None, rec: ValidationRecord) -> None:
        """Adopt a newly funded account into the submission pool if it's viable."""
        if p_live is None:
            return
        w = getattr(p_live, "wallet", None)
        if w is None:
            return
        funded_drops = 0
        tx_amount = (p_live.tx_json or {}).get("Amount")
        if isinstance(tx_amount, str):
            funded_drops = int(tx_amount)
        if self._is_viable_for_pool({"XRP": float(funded_drops)}):
            self.wallets[w.address] = w
            self._record_for(w.address)
            self.users.append(w)
            self.save_wallet_to_store(w, is_user=True, funded_ledger_index=rec.seq)
            self.update_txn_context()
            log.debug("Adopted new account %s (funded: %d drops)", w.address, funded_drops)
        else:
            log.debug(
                "Skipped adopting %s — funded amount %d drops insufficient for any configured payment "
                "(need > %d drops for XRP payment + reserve + fees)",
                w.address,
                funded_drops,
                int(self.config.get("transactions", {}).get("payment", {}).get("amount", 0)) + 2_010_000,
            )

    def _on_payment_validated(self, p_live: PendingTx | None, meta_result: str | None) -> None:
        """Update in-memory balances for a successful Payment."""
        if not (p_live and meta_result == "tesSUCCESS" and p_live.transaction_type == C.TxType.PAYMENT):
            return
        try:
            tx_json = p_live.tx_json
            if not tx_json:
                return
            sender = tx_json.get("Account")
            destination = tx_json.get("Destination")
            amount = tx_json.get("Amount")
            if not (sender and destination and amount):
                return
            if isinstance(amount, str):
                amount_val = float(amount)
                self._update_balance(sender, "XRP", -amount_val)
                self._update_balance(destination, "XRP", amount_val)
            elif isinstance(amount, dict):
                currency = amount.get("currency")
                issuer = amount.get("issuer")
                value = float(amount.get("value", 0))
                if currency and issuer:
                    if sender != issuer:
                        self._update_balance(sender, currency, -value, issuer)
                    if destination != issuer:
                        self._update_balance(destination, currency, value, issuer)
                    log.debug("Balance update: %s -> %s: %s %s", sender, destination, value, currency)
        except Exception as e:
            log.debug("Failed to update in-memory balances for %s: %s", p_live.tx_hash, e)

    async def _on_mptoken_created(self, p_live: PendingTx | None, rec: ValidationRecord) -> None:
        """Track the new MPToken issuance ID after a successful MPTokenIssuanceCreate."""
        if not (p_live and p_live.transaction_type == C.TxType.MPTOKEN_ISSUANCE_CREATE):
            return
        try:
            tx_result = await self._rpc(Tx(transaction=rec.txn))
            mpt_id = tx_result.result.get("mpt_issuance_id")
            if mpt_id and mpt_id not in self._mptoken_issuance_ids:
                self._mptoken_issuance_ids.append(mpt_id)
                self.update_txn_context()
                log.debug("Tracked new MPToken issuance ID: %s", mpt_id)
        except Exception as e:
            log.warning("Failed to extract MPToken issuance ID from %s: %s", rec.txn, e)

    async def _on_batch_validated(self, p_live: PendingTx | None) -> None:
        """Sync account sequence from ledger after a Batch transaction validates."""
        if not (p_live and p_live.transaction_type == C.TxType.BATCH and p_live.account):
            return
        try:
            ai = await self._rpc(AccountInfo(account=p_live.account, ledger_index="validated"))
            rec_acct = self._record_for(p_live.account)
            async with rec_acct.lock:
                old_seq = rec_acct.next_seq
                rec_acct.next_seq = ai.result["account_data"]["Sequence"]
                log.debug("Batch validated: synced %s sequence %s -> %s", p_live.account, old_seq, rec_acct.next_seq)
        except Exception as e:
            log.warning("Failed to sync sequence after Batch validation: %s", e)

    def _on_amm_created(self, p_live: PendingTx | None, meta_result: str | None) -> None:
        """Register a new AMM pool after a successful AMMCreate."""
        if not (p_live and meta_result == "tesSUCCESS" and p_live.transaction_type == C.TxType.AMM_CREATE):
            return
        try:
            tx_json = p_live.tx_json
            if not tx_json:
                return
            amount1 = tx_json.get("Amount")
            amount2 = tx_json.get("Amount2")
            if not (amount1 and amount2):
                return
            asset1 = (
                {"currency": "XRP"}
                if isinstance(amount1, str)
                else {"currency": amount1["currency"], "issuer": amount1["issuer"]}
            )
            asset2 = (
                {"currency": "XRP"}
                if isinstance(amount2, str)
                else {"currency": amount2["currency"], "issuer": amount2["issuer"]}
            )
            self._register_amm_pool(asset1, asset2, p_live.account)
        except Exception as e:
            log.warning("Failed to register AMM pool from %s: %s", p_live.tx_hash, e)

    def _on_dex_activity(self, p_live: PendingTx | None, meta_result: str | None) -> None:
        """Update DEX metrics counters for successful AMM deposit/withdraw and offer creation."""
        if not (p_live and meta_result == "tesSUCCESS"):
            return
        if p_live.transaction_type == C.TxType.AMM_DEPOSIT:
            self.dex_metrics.total_deposits += 1
            tx_json = p_live.tx_json or {}
            dep_asset1 = tx_json.get("Asset")
            dep_asset2 = tx_json.get("Asset2")
            if dep_asset1 and dep_asset2 and p_live.account:
                self.amm.add_lp_holder(dep_asset1, dep_asset2, p_live.account)
        elif p_live.transaction_type == C.TxType.AMM_WITHDRAW:
            self.dex_metrics.total_withdrawals += 1
        elif p_live.transaction_type == C.TxType.OFFER_CREATE:
            self.dex_metrics.total_offers += 1

    # ------------------------------------------------------------------
    # Credential hooks
    # ------------------------------------------------------------------
    def _on_credential_created(self, p_live: PendingTx | None) -> None:
        if not (p_live and p_live.transaction_type == C.TxType.CREDENTIAL_CREATE):
            return
        tx_json = p_live.tx_json or {}
        cred = {
            "issuer": tx_json.get("Account"),
            "subject": tx_json.get("Subject"),
            "credential_type": tx_json.get("CredentialType"),
            "accepted": False,
        }
        if cred["issuer"] and cred["subject"] and cred["credential_type"]:
            self._credentials.append(cred)
            self.update_txn_context()
            log.debug("Tracked credential: issuer=%s subject=%s", cred["issuer"], cred["subject"])

    def _on_credential_accepted(self, p_live: PendingTx | None) -> None:
        if not (p_live and p_live.transaction_type == C.TxType.CREDENTIAL_ACCEPT):
            return
        tx_json = p_live.tx_json or {}
        issuer = tx_json.get("Issuer")
        subject = tx_json.get("Account")  # The acceptor is the subject
        cred_type = tx_json.get("CredentialType")
        for c in self._credentials:
            if c["issuer"] == issuer and c["subject"] == subject and c["credential_type"] == cred_type:
                c["accepted"] = True
                break

    def _on_credential_deleted(self, p_live: PendingTx | None) -> None:
        if not (p_live and p_live.transaction_type == C.TxType.CREDENTIAL_DELETE):
            return
        tx_json = p_live.tx_json or {}
        issuer = tx_json.get("Issuer")
        subject = tx_json.get("Subject")
        cred_type = tx_json.get("CredentialType")
        self._credentials = [
            c
            for c in self._credentials
            if not (c["issuer"] == issuer and c["subject"] == subject and c["credential_type"] == cred_type)
        ]
        self.update_txn_context()

    # ------------------------------------------------------------------
    # Vault hooks
    # ------------------------------------------------------------------
    async def _on_vault_created(self, p_live: PendingTx | None, rec: ValidationRecord) -> None:
        if not (p_live and p_live.transaction_type == C.TxType.VAULT_CREATE):
            return
        try:
            tx_result = await self._rpc(Tx(transaction=rec.txn))
            meta = tx_result.result.get("meta", {})
            for node in meta.get("AffectedNodes", []):
                created = node.get("CreatedNode", {})
                if created.get("LedgerEntryType") == "Vault":
                    vault_id = created.get("LedgerIndex")
                    if vault_id:
                        tx_json = p_live.tx_json or {}
                        vault = {
                            "vault_id": vault_id,
                            "owner": p_live.account,
                            "asset": tx_json.get("Asset"),
                        }
                        self._vaults.append(vault)
                        self.update_txn_context()
                        log.debug("Tracked vault: %s owner=%s", vault_id, p_live.account)
                        return
        except Exception as e:
            log.warning("Failed to extract vault ID from %s: %s", rec.txn, e)

    def _on_vault_deleted(self, p_live: PendingTx | None) -> None:
        if not (p_live and p_live.transaction_type == C.TxType.VAULT_DELETE):
            return
        tx_json = p_live.tx_json or {}
        vault_id = tx_json.get("VaultID")
        if vault_id:
            self._vaults = [v for v in self._vaults if v["vault_id"] != vault_id]
            self.update_txn_context()

    # ------------------------------------------------------------------
    # Permissioned Domain hooks
    # ------------------------------------------------------------------
    async def _on_domain_created(self, p_live: PendingTx | None, rec: ValidationRecord) -> None:
        if not (p_live and p_live.transaction_type == C.TxType.PERMISSIONED_DOMAIN_SET):
            return
        # If DomainID is present, this is an update — not a new domain
        if (p_live.tx_json or {}).get("DomainID"):
            return
        try:
            tx_result = await self._rpc(Tx(transaction=rec.txn))
            meta = tx_result.result.get("meta", {})
            for node in meta.get("AffectedNodes", []):
                created = node.get("CreatedNode", {})
                if created.get("LedgerEntryType") == "PermissionedDomain":
                    domain_id = created.get("LedgerIndex")
                    if domain_id:
                        domain = {"domain_id": domain_id, "owner": p_live.account}
                        self._domains.append(domain)
                        self.update_txn_context()
                        log.debug("Tracked domain: %s owner=%s", domain_id, p_live.account)
                        return
        except Exception as e:
            log.warning("Failed to extract domain ID from %s: %s", rec.txn, e)

    def _on_domain_deleted(self, p_live: PendingTx | None) -> None:
        if not (p_live and p_live.transaction_type == C.TxType.PERMISSIONED_DOMAIN_DELETE):
            return
        tx_json = p_live.tx_json or {}
        domain_id = tx_json.get("DomainID")
        if domain_id:
            self._domains = [d for d in self._domains if d["domain_id"] != domain_id]
            self.update_txn_context()

    async def _cascade_expire_account(
        self, account: str, failed_seq: int, exclude_hash: str | None = None, fetch_seq_from_ledger: bool = False
    ):
        """Cascade-expire all pending txns for account with sequence > failed_seq, and reset next_seq.

        Called when a transaction fails with terPRE_SEQ or expires - all higher-sequence
        txns for the same account are doomed and should be expired immediately.

        Args:
            fetch_seq_from_ledger: If True, fetch next_seq from ledger (for terPRE_SEQ where we
                don't know what ledger expects). If False, set to failed_seq (for expiry where
                we know ledger still expects that sequence).
        """
        cascade_count = 0
        for tx_hash, p in list(self.pending.items()):
            if (
                tx_hash != exclude_hash
                and p.account == account
                and p.sequence is not None
                and p.sequence > failed_seq
                and p.state not in C.TERMINAL_STATE
            ):
                p.state = C.TxState.EXPIRED
                self._total_expired += 1
                if p.engine_result_first is None:
                    p.engine_result_first = "CASCADE_EXPIRED"
                await self.store.mark(
                    tx_hash,
                    state=C.TxState.EXPIRED,
                    account=p.account,
                    sequence=p.sequence,
                    transaction_type=p.transaction_type,
                    engine_result_first=p.engine_result_first,
                )
                cascade_count += 1

        # Count remaining pending txns for this account after cascade
        remaining = sum(1 for p in self.pending.values() if p.account == account and p.state in C.PENDING_STATES)
        log.debug(
            "Cascade check for %s: expired %d txns with seq > %d, %d still pending",
            account,
            cascade_count,
            failed_seq,
            remaining,
        )

        rec = self._record_for(account)
        async with rec.lock:
            rec.generation += 1
            old_seq = rec.next_seq
            if fetch_seq_from_ledger:
                try:
                    ai = await self._rpc(AccountInfo(account=account, ledger_index="validated"))
                    new_seq = ai.result["account_data"]["Sequence"]
                    rec.next_seq = new_seq
                    delta = new_seq - old_seq if old_seq else None
                    if delta is None or delta != 0:
                        log.debug(
                            f"SEQ RESET (ledger): {account} {old_seq} -> {new_seq} (delta: {delta if delta is not None else 'N/A'})"
                        )
                    else:
                        log.debug(f"SEQ RESET (ledger, no-op): {account} {old_seq} -> {new_seq} (delta: 0)")
                except Exception as e:
                    log.error(f"Failed to fetch sequence for {account}: {e}")
                    rec.next_seq = failed_seq  # Best guess fallback
                    delta = failed_seq - old_seq if old_seq else None
                    log.warning(
                        f"SEQ RESET (fallback): {account} {old_seq} -> {failed_seq} (delta: {delta if delta is not None else 'N/A'})"
                    )
            else:
                rec.next_seq = failed_seq
                delta = failed_seq - old_seq if old_seq else None
                if delta is None or delta != 0:
                    log.debug(
                        f"SEQ RESET (expiry): {account} {old_seq} -> {failed_seq} (delta: {delta if delta is not None else 'N/A'})"
                    )
                else:
                    log.debug(f"SEQ RESET (expiry, no-op): {account} {old_seq} -> {failed_seq} (delta: 0)")

    async def record_expired(self, tx_hash: str, *, cascade: bool = True):
        if tx_hash not in self.pending:
            return

        p = self.pending[tx_hash]
        p.state = C.TxState.EXPIRED
        p.finalized_at = time.time()
        self._total_expired += 1
        await self.store.mark(
            tx_hash,
            state=C.TxState.EXPIRED,
            account=p.account,
            sequence=p.sequence,
            transaction_type=p.transaction_type,
            engine_result_first=p.engine_result_first,
        )

        if cascade and p.account and p.sequence is not None:
            if p.transaction_type == C.TxType.BATCH:
                log.debug(
                    f"EXPIRED (Batch): {p.transaction_type} account={p.account} seq={p.sequence} hash={tx_hash} - will cascade and sync from ledger"
                )
            else:
                log.debug(
                    f"EXPIRED: {p.transaction_type} account={p.account} seq={p.sequence} hash={tx_hash} - will cascade and sync from ledger"
                )
            await self._cascade_expire_account(p.account, p.sequence, exclude_hash=tx_hash, fetch_seq_from_ledger=True)

    def _is_viable_for_pool(self, balances: dict) -> bool:
        """Return True if an account has enough balance to send at least one transaction.

        Checks each asset class against its configured minimum send amount plus
        a small fee/reserve buffer. Accounts that can't afford anything should
        not be added to the submission pool.

        Args:
            balances: Dict of {asset_key: amount} where asset_key is:
                - "XRP"             → amount in drops (float/int)
                - (currency, issuer) → IOU amount (float)
                - ("MPT", mpt_id)   → MPToken amount (float)

        Returns:
            True if the account can afford at least one configured transaction type.
        """
        BASE_RESERVE_DROPS = 2_000_000  # 2 XRP minimum account reserve
        FEE_BUFFER_DROPS = 10_000  # headroom for fees on a few txns

        # XRP check: must cover reserve + fee buffer + at least one payment
        xrp_balance = balances.get("XRP", 0.0)
        xrp_payment = int(self.config.get("transactions", {}).get("payment", {}).get("amount", 0))
        if xrp_balance >= BASE_RESERVE_DROPS + FEE_BUFFER_DROPS + xrp_payment:
            return True

        # IOU check: any non-zero IOU balance is sufficient to attempt a payment
        for key, amount in balances.items():
            if isinstance(key, tuple) and len(key) == 2 and key[0] != "MPT" and amount > 0:
                return True

        # MPToken check: any non-zero MPT balance
        for key, amount in balances.items():
            if isinstance(key, tuple) and len(key) == 2 and key[0] == "MPT" and amount > 0:
                return True

        return False

    def expire_past_lls(self, ledger_index: int) -> int:
        """Force-expire pending txns whose LastLedgerSequence has passed.

        This is the producer's self-healing mechanism: when all accounts are blocked
        by stale pending txns (e.g. after tefPAST_SEQ cascades), this scans and expires
        them immediately rather than waiting for the 5s periodic_finality_check.

        Returns count of expired txns.
        """
        expired_count = 0
        for tx_hash, p in list(self.pending.items()):
            if p.state in C.PENDING_STATES and p.last_ledger_seq and p.last_ledger_seq < ledger_index:
                p.state = C.TxState.EXPIRED
                self._total_expired += 1
                if p.engine_result_first is None:
                    p.engine_result_first = "PAST_LLS"
                p.finalized_at = time.time()
                expired_count += 1
        if expired_count:
            log.warning(
                "expire_past_lls: force-expired %d stale txns (ledger=%d)",
                expired_count,
                ledger_index,
            )
        return expired_count

    def diagnostics_snapshot(self) -> dict:
        """Return diagnostic data about pending txn and account health."""
        pending_by_state: dict[str, int] = {}
        oldest_pending_age = 0  # in ledgers
        blocked_accounts: set[str] = set()

        for p in self.pending.values():
            if p.state in C.PENDING_STATES:
                state_name = p.state.value
                pending_by_state[state_name] = pending_by_state.get(state_name, 0) + 1
                blocked_accounts.add(p.account)
                if self.latest_ledger_index and p.created_ledger:
                    age = self.latest_ledger_index - p.created_ledger
                    oldest_pending_age = max(oldest_pending_age, age)

        total_accounts = len(self.wallets)
        free_count = total_accounts - len(blocked_accounts)

        # Sample of blocked accounts with their pending details
        blocked_sample: list[dict] = []
        for addr in list(blocked_accounts)[:10]:
            acct_pending = [
                {
                    "hash": p.tx_hash,
                    "state": p.state.value,
                    "type": p.transaction_type,
                    "seq": p.sequence,
                    "lls": p.last_ledger_seq,
                    "age_ledgers": self.latest_ledger_index - p.created_ledger if p.created_ledger else None,
                    "past_lls": p.last_ledger_seq < self.latest_ledger_index if p.last_ledger_seq else False,
                }
                for p in self.pending.values()
                if p.account == addr and p.state in C.PENDING_STATES
            ]
            blocked_sample.append({"account": addr, "pending": acct_pending})

        return {
            "ledger_index": self.latest_ledger_index,
            "total_accounts": total_accounts,
            "blocked_accounts": len(blocked_accounts),
            "free_accounts": free_count,
            "pending_by_state": pending_by_state,
            "oldest_pending_age_ledgers": oldest_pending_age,
            "blocked_sample": blocked_sample,
        }

    def find_by_state(self, *states: C.TxState) -> list[PendingTx]:
        return [p for p in self.pending.values() if p.state in set(states)]

    def get_pending_txn_counts_by_account(self) -> dict[str, int]:
        """Get count of pending transactions per account.

        Returns:
            Dict mapping account address to count of CREATED/SUBMITTED/RETRYABLE transactions.
            Used to enforce per-account queue limit of 10 (see FeeEscalation.md:260).
        """
        counts = {}
        for p in self.pending.values():
            if p.state in C.PENDING_STATES and p.account:
                counts[p.account] = counts.get(p.account, 0) + 1
        return counts

    async def build_sign_and_track(
        self,
        txn: Transaction,
        wallet: Wallet,
        horizon: int = C.HORIZON,
        *,
        fee_drops: int | None = None,
        created_ledger: int | None = None,
        last_ledger_seq: int | None = None,
        preallocated_seq: int | None = None,
    ) -> PendingTx:
        if created_ledger is not None and last_ledger_seq is not None:
            created_li = created_ledger
            lls = last_ledger_seq
        else:
            created_li = (await self._rpc(ServerState(), t=2.0)).result["state"]["validated_ledger"]["seq"]
            lls = created_li + horizon

        tx = txn.to_xrpl()
        if tx.get("Flags") == 0:
            del tx["Flags"]

        need_seq = "TicketSequence" not in tx and not tx.get("Sequence")
        need_fee = not tx.get("Fee")

        if need_seq:
            if preallocated_seq is not None:
                seq = preallocated_seq
            else:
                seq = await self.alloc_seq(wallet.address)
            # In asyncio (cooperative): no await between here and seq assignment,
            # so generation reflects the state at alloc_seq time.
            acct_gen = self._record_for(wallet.address).generation
        else:
            seq = tx.get("Sequence")
            acct_gen = 0  # Ticket-based tx; generation not applicable

        base_fee = (
            fee_drops
            if (fee_drops is not None and need_fee)
            else (await self._open_ledger_fee() if need_fee else int(tx["Fee"]))
        )

        txn_type = tx.get("TransactionType")
        if txn_type == "Batch":
            OWNER_RESERVE_DROPS = 2_000_000
            inner_txns = tx.get("RawTransactions", [])
            fee = (2 * OWNER_RESERVE_DROPS) + (base_fee * len(inner_txns))
            log.debug(f"Batch fee: 2*{OWNER_RESERVE_DROPS} + {base_fee}*{len(inner_txns)} = {fee} drops")
        elif txn_type in ("AMMCreate", "VaultCreate", "PermissionedDomainSet"):
            OWNER_RESERVE_DROPS = 2_000_000
            fee = OWNER_RESERVE_DROPS
            log.debug(f"{txn_type} fee: {fee} drops (owner_reserve)")
        else:
            fee = base_fee

        if need_seq:
            tx["Sequence"] = seq
        if need_fee:
            tx["Fee"] = str(fee)
        tx["SigningPubKey"] = wallet.public_key
        tx["LastLedgerSequence"] = lls

        signing_blob = encode_for_signing(tx)
        to_sign = signing_blob if isinstance(signing_blob, str) else signing_blob.hex()
        tx["TxnSignature"] = sign(to_sign, wallet.private_key)
        signed_blob_hex = encode(tx)
        local_txid = _txid_from_signed_blob_hex(signed_blob_hex)

        p = PendingTx(
            tx_hash=local_txid,
            signed_blob_hex=signed_blob_hex,
            account=tx["Account"],
            tx_json=tx,
            sequence=tx.get("Sequence"),
            last_ledger_seq=lls,
            transaction_type=tx.get("TransactionType"),
            created_ledger=created_li,
            account_generation=acct_gen,
        )
        await self.record_created(p)
        return p

    async def submit_pending(self, p: PendingTx, timeout: float = C.SUBMIT_TIMEOUT) -> dict | None:
        if p.state in C.TERMINAL_STATE:
            log.debug("%s not active txn!", p)
            return None

        try:
            p.attempts += 1
            log.debug(
                "submit start \n\ttransaction_type=%s\n\tstate=%s\n\tattempts=%s\n\taccount=%s\n\tseq=%s\n\ttx=%s",
                p.transaction_type,
                p.state.name,
                p.attempts,
                p.account,
                p.sequence,
                p.tx_hash,
            )
            resp = await asyncio.wait_for(self.client.request(SubmitOnly(tx_blob=p.signed_blob_hex)), timeout=timeout)
            res = resp.result
            er = res.get("engine_result")
            if p.engine_result_first is None:
                p.engine_result_first = er
            if p.engine_result_message is None:
                p.engine_result_message = res.get("engine_result_message")

            if isinstance(er, str) and er.startswith("tel"):
                p.state = C.TxState.SUBMITTED
                self.pending[p.tx_hash] = p
                await self.store.mark(
                    p.tx_hash,
                    state=p.state,
                    account=p.account,
                    sequence=p.sequence,
                    transaction_type=p.transaction_type,
                    engine_result_first=p.engine_result_first,
                )
                log.debug(f"tel* (may retry): {er} - {p.transaction_type} seq={p.sequence} - tracking until expiry")
                return res

            if er == "tefPAST_SEQ":
                p.state = C.TxState.REJECTED
                self._total_rejected += 1
                if p.transaction_type:
                    tx_rejected(p.transaction_type, er, details={"hash": p.tx_hash, "account": p.account, "sequence": p.sequence})
                self.pending[p.tx_hash] = p
                await self.store.mark(
                    p.tx_hash,
                    state=p.state,
                    account=p.account,
                    sequence=p.sequence,
                    transaction_type=p.transaction_type,
                    engine_result_first=p.engine_result_first,
                    engine_result_final=er,
                )
                # Only cascade + resync if this is the first tefPAST_SEQ for
                # this account (i.e., its next_seq hasn't already been reset
                # below this txn's sequence by a prior cascade).
                rec = self._record_for(p.account)
                try:
                    _ai = await self._rpc(AccountInfo(account=p.account, ledger_index="current"))
                    _actual = _ai.result["account_data"]["Sequence"]
                    log.debug(
                        "tefPAST_SEQ detail: %s %s submitted_seq=%d current_ledger_seq=%d delta=%d next_seq_before=%s",
                        p.transaction_type,
                        p.account,
                        p.sequence,
                        _actual,
                        _actual - p.sequence,
                        rec.next_seq,
                    )
                except Exception as _e:
                    log.debug("tefPAST_SEQ detail: fetch failed: %s", _e)

                if rec.next_seq is not None and rec.next_seq > p.sequence:
                    log.debug(
                        f"tefPAST_SEQ (cascade victim): {p.transaction_type} account={p.account} seq={p.sequence} - next_seq={rec.next_seq}, forcing ledger resync"
                    )
                    await self._cascade_expire_account(
                        p.account, p.sequence, exclude_hash=p.tx_hash, fetch_seq_from_ledger=True
                    )
                else:
                    log.debug(
                        f"tefPAST_SEQ: {p.transaction_type} account={p.account} seq={p.sequence} - resetting from ledger"
                    )
                    await self._cascade_expire_account(
                        p.account, p.sequence, exclude_hash=p.tx_hash, fetch_seq_from_ledger=True
                    )
                return res

            if isinstance(er, str) and er.startswith(("tem", "tef")):
                p.state = C.TxState.REJECTED
                self._total_rejected += 1
                if p.transaction_type:
                    tx_rejected(p.transaction_type, er, details={"hash": p.tx_hash, "account": p.account, "sequence": p.sequence})
                self.pending[p.tx_hash] = p
                await self.store.mark(
                    p.tx_hash,
                    state=p.state,
                    account=p.account,
                    sequence=p.sequence,
                    transaction_type=p.transaction_type,
                    engine_result_first=p.engine_result_first,
                    engine_result_final=er,
                )
                if er == "temDISABLED":
                    log.debug(f"REJECTED: {er} - {p.transaction_type} from {p.account} (amendment not enabled)")
                else:
                    log.warning(f"REJECTED: {er} - {p.transaction_type} from {p.account} seq={p.sequence} hash={p.tx_hash}")

                if p.transaction_type == C.TxType.BATCH and p.account:
                    try:
                        ai = await self._rpc(AccountInfo(account=p.account, ledger_index="validated"))
                        rec_acct = self._record_for(p.account)
                        async with rec_acct.lock:
                            old_seq = rec_acct.next_seq
                            rec_acct.next_seq = ai.result["account_data"]["Sequence"]
                            log.debug(
                                f"Batch rejected: synced {p.account} sequence {old_seq} -> {rec_acct.next_seq}"
                            )
                    except Exception as e:
                        log.warning(f"Failed to sync sequence after Batch rejection: {e}")

                return res

            if er == "terPRE_SEQ":
                log.debug(
                    f"terPRE_SEQ: {p.transaction_type} account={p.account} seq={p.sequence} hash={p.tx_hash} - cascade expire and ledger resync"
                )
                p.state = C.TxState.EXPIRED
                self._total_expired += 1
                self.pending[p.tx_hash] = p
                await self.store.mark(
                    p.tx_hash,
                    state=p.state,
                    account=p.account,
                    sequence=p.sequence,
                    transaction_type=p.transaction_type,
                    engine_result_first=er,
                )
                await self._cascade_expire_account(p.account, p.sequence, fetch_seq_from_ledger=True)
                return res

            srv_txid = res.get("tx_json", {}).get("hash")
            if isinstance(srv_txid, str) and srv_txid and srv_txid != p.tx_hash:
                self.pending[srv_txid] = self.pending.pop(p.tx_hash, p)
                p.tx_hash = srv_txid
            await self.record_submitted(p, engine_result=er, srv_txid=srv_txid)
            return res

        except asyncio.TimeoutError:
            p.state = C.TxState.FAILED_NET
            self.pending[p.tx_hash] = p
            log.error("timeout")
            await self.store.mark(
                p.tx_hash,
                state=C.TxState.FAILED_NET,
                account=p.account,
                sequence=p.sequence,
                transaction_type=p.transaction_type,
                engine_result_first=p.engine_result_first,
            )
            return {"engine_result": "timeout"}

        except Exception as e:
            p.state = C.TxState.FAILED_NET
            self.pending[p.tx_hash] = p
            log.error("submit error tx=%s: %s", p.tx_hash, e)
            await self.store.mark(
                p.tx_hash,
                state=C.TxState.FAILED_NET,
                account=p.account,
                sequence=p.sequence,
                transaction_type=p.transaction_type,
                message=str(e),
            )
            return {"engine_result": "error", "message": str(e)}

    async def check_finality(self, p: PendingTx, grace: int = 2) -> tuple[C.TxState, int | None]:
        try:
            txr = await self.client.request(Tx(transaction=p.tx_hash))
            if txr.is_successful() and txr.result.get("validated"):
                li = int(txr.result["ledger_index"])
                result = txr.result["meta"]["TransactionResult"]

                p.state = C.TxState.VALIDATED
                p.validated_ledger = li
                p.meta_txn_result = result
                await self.record_validated(ValidationRecord(p.tx_hash, li, ValidationSrc.POLL), result)
                return p.state, li
        except Exception:
            log.error("check_finality failed for %s", p.tx_hash, exc_info=True)

        latest_val = await self._latest_validated_ledger()
        if latest_val > (p.last_ledger_seq + grace):
            await self.record_expired(p.tx_hash)
            return p.state, None

        # Don't transition FAILED_NET → RETRYABLE: the tx may still be queued in rippled.
        # Keep it locked (pending) until LLS expires or WS confirms validation.
        if p.state not in (C.TxState.SUBMITTED, C.TxState.FAILED_NET):
            p.state = C.TxState.RETRYABLE
            await self.store.mark(p.tx_hash, state=p.state)
        return p.state, None

    async def submit_random_txn(self):
        txn = await generate_txn(self.ctx)
        if txn is None:
            return None
        pending_txn = await self.build_sign_and_track(txn, self.wallets[txn.account])
        x = await self.submit_pending(pending_txn)
        log.debug(f"Submitting random {txn.transaction_type.name.title().replace('_', ' ')} txn.")
        log.debug(x)
        return x

    async def create_transaction(self, transaction: str):
        """
        Build, sign, track, and submit a transaction.
        If auto_expand_on_payment is True and this is a Payment, we replace the destination
        with a new wallet we control, then adopt it into the pool after validation.
        """
        txn = await generate_txn(self.ctx, transaction)
        if txn is None:
            return {"error": f"Cannot build {transaction} — no eligible accounts or missing prerequisites"}
        pending = await self.build_sign_and_track(txn, self.wallets[txn.account])
        return await self.submit_pending(pending)

    async def create_account(self, initial_xrp_drops: str | None = None, wait=True) -> dict[str, Any]:
        """
        Randomly generate a wallet, fund it from funding_wallet, and adopt it
        into the pool *after* validation via record_validated().
        Returns basic submission info (tx hash) so callers can monitor if desired.
        """
        if not getattr(self, "funding_wallet", None):
            raise RuntimeError("No funding_wallet configured")

        amount = initial_xrp_drops or self.config["users"]["default_balance"]

        w = Wallet.create()

        fund_txn = Payment(
            account=self.funding_wallet.address,
            destination=w.address,
            amount=str(amount),
        )

        pending = await self.build_sign_and_track(fund_txn, self.funding_wallet)
        pending.wallet = w  # stash for *post-validation* adoption in record_validated()
        submit_res = await self.submit_pending(pending)

        return {
            "address": w.address,
            "tx_hash": pending.tx_hash,
            "submitted": True,
            "engine_result": (submit_res or {}).get("engine_result"),
            "funding_drops": int(amount),
        }

    async def _submit_batched_by_account(
        self, all_pending: list[PendingTx], label: str, max_per_account: int = 10
    ) -> dict:
        """Submit transactions in batches, respecting per-account queue limits.

        Groups transactions by account and submits at most max_per_account per account
        at a time. Waits for validations before submitting more from the same account.

        Returns dict of result counts.
        """
        from collections import defaultdict

        by_account: dict[str, list[PendingTx]] = defaultdict(list)
        for p in all_pending:
            by_account[p.account].append(p)

        total = len(all_pending)
        result_counts: dict[str, int] = {}
        submitted_count = 0

        account_idx: dict[str, int] = {acc: 0 for acc in by_account}
        account_inflight: dict[str, int] = {acc: 0 for acc in by_account}

        while submitted_count < total or any(account_inflight[acc] > 0 for acc in by_account):
            to_submit = []
            for acc, txns in by_account.items():
                idx = account_idx[acc]
                inflight = account_inflight[acc]
                available_slots = max_per_account - inflight

                while available_slots > 0 and idx < len(txns):
                    to_submit.append(txns[idx])
                    account_inflight[acc] += 1
                    idx += 1
                    available_slots -= 1
                    submitted_count += 1

                account_idx[acc] = idx

            if to_submit:
                log.info(f"  {label}: Submitting batch of {len(to_submit)} txns ({submitted_count}/{total} total)")

                async with asyncio.TaskGroup() as tg:
                    submit_tasks = [tg.create_task(self.submit_pending(p)) for p in to_submit]

                for task in submit_tasks:
                    try:
                        result = task.result()
                        er = result.get("engine_result") if result else "None"
                        result_counts[er] = result_counts.get(er, 0) + 1
                    except Exception as e:
                        log.error(f"{label} submission failed: {e}")
                        result_counts["ERROR"] = result_counts.get("ERROR", 0) + 1

            current = await self._current_ledger_index()
            while await self._current_ledger_index() <= current:
                await asyncio.sleep(0.3)

            for acc, txns in by_account.items():
                terminal = sum(1 for p in txns if p.state in C.TERMINAL_STATE)
                submitted_for_acc = account_idx[acc]
                account_inflight[acc] = submitted_for_acc - terminal

            all_terminal = all(p.state in C.TERMINAL_STATE for p in all_pending)
            if all_terminal:
                break

        return result_counts

    async def _wait_all_validated(
        self,
        pending_list: list[PendingTx],
        label: str,
        timeout: float = 120.0,
    ) -> dict[str, int]:
        """Wait for all transactions to reach terminal state.

        Polls check_finality() for each pending tx until all are terminal
        (VALIDATED, REJECTED, EXPIRED) or timeout.

        Returns dict with counts by final state.
        """
        start = time.time()
        while time.time() - start < timeout:
            for p in pending_list:
                if p.state not in C.TERMINAL_STATE:
                    try:
                        await self.check_finality(p)
                    except Exception as e:
                        log.debug(f"check_finality failed for {p.tx_hash}: {e}")

            counts: dict[str, int] = {}
            for p in pending_list:
                state = p.state.name
                counts[state] = counts.get(state, 0) + 1

            if all(p.state in C.TERMINAL_STATE for p in pending_list):
                validated = counts.get("VALIDATED", 0)
                total = len(pending_list)
                log.info(f"{label} complete: {validated}/{total} validated, {counts}")
                return counts

            await asyncio.sleep(0.5)

        counts = {}
        for p in pending_list:
            state = p.state.name
            counts[state] = counts.get(state, 0) + 1
        log.warning(f"{label} timeout after {timeout}s: {counts}")
        return counts

    async def _submit_and_wait_batched(
        self,
        pending_list: list[PendingTx],
        label: str,
        max_per_account: int = 10,
        max_batch_size: int | None = None,
        timeout: float = 180.0,
    ) -> dict[str, int]:
        """Submit transactions in batches respecting per-account AND global limits.

        Groups by account, submits max_per_account at a time per account,
        BUT also limits total in-flight to max_batch_size to avoid queue overflow.

        Returns dict with counts by final state.
        """
        from collections import defaultdict

        by_account: dict[str, list[PendingTx]] = defaultdict(list)
        for p in pending_list:
            by_account[p.account].append(p)

        total = len(pending_list)
        submitted_hashes: set[str] = set()

        if max_batch_size is None:
            try:
                max_batch_size = await self._expected_ledger_size()
            except Exception:
                max_batch_size = 50

        log.info(f"{label}: {total} txns across {len(by_account)} accounts (max {max_batch_size}/batch)")

        start = time.time()
        while time.time() - start < timeout:
            total_inflight = sum(
                1 for p in pending_list if p.tx_hash in submitted_hashes and p.state not in C.TERMINAL_STATE
            )
            global_slots = max_batch_size - total_inflight

            if global_slots <= 0:
                await asyncio.sleep(0.5)
                for p in pending_list:
                    if p.tx_hash in submitted_hashes and p.state not in C.TERMINAL_STATE:
                        try:
                            await self.check_finality(p)
                        except Exception:
                            pass
                continue

            newly_submitted = []
            for acc, txns in by_account.items():
                if len(newly_submitted) >= global_slots:
                    break  # Hit global limit

                inflight = sum(1 for p in txns if p.tx_hash in submitted_hashes and p.state not in C.TERMINAL_STATE)
                available_slots = min(max_per_account - inflight, global_slots - len(newly_submitted))

                for p in txns:
                    if available_slots <= 0:
                        break
                    if p.tx_hash not in submitted_hashes:
                        newly_submitted.append(p)
                        submitted_hashes.add(p.tx_hash)
                        available_slots -= 1

            if newly_submitted:
                log.info(f"  {label}: Submitting {len(newly_submitted)} txns ({len(submitted_hashes)}/{total} total)")
                for p in newly_submitted:
                    try:
                        await self.submit_pending(p)
                    except Exception as e:
                        log.error(f"Submit failed for {p.tx_hash}: {e}")

            for p in pending_list:
                if p.tx_hash in submitted_hashes and p.state not in C.TERMINAL_STATE:
                    try:
                        await self.check_finality(p)
                    except Exception:
                        pass

            counts: dict[str, int] = {}
            for p in pending_list:
                state = p.state.name
                counts[state] = counts.get(state, 0) + 1

            if all(p.state in C.TERMINAL_STATE for p in pending_list):
                validated = counts.get("VALIDATED", 0)
                log.info(f"{label} complete: {validated}/{total} validated, {counts}")
                return counts

            await asyncio.sleep(0.5)

        counts = {}
        for p in pending_list:
            state = p.state.name
            counts[state] = counts.get(state, 0) + 1
        log.warning(f"{label} timeout after {timeout}s: {counts}")
        return counts

    async def _init_batch(
        self,
        txn_wallet_pairs: list[tuple[Transaction, Wallet]],
        label: str,
        batch_size: int | None = None,  # None = use dynamic expected_ledger_size
    ) -> tuple[int, int, list[PendingTx]]:
        """Aggressive init helper: submit txns continuously, avoiding per-account sequence conflicts.

        Submits transactions in parallel via TaskGroup, but tracks per-account pending state
        to avoid submitting multiple txns from the same account before validation.
        Keeps submitting aggressively without waiting for entire batches to complete.

        Args:
            batch_size: Fixed batch size, or None for dynamic (expected_ledger_size)

        Returns: (validated_count, total_count, all_pending_txns)
        """
        total = len(txn_wallet_pairs)
        all_pending: list[PendingTx] = []
        submit_failed = 0

        log.info(f"{label}: {total} transactions (aggressive submission, tracking per-account pending)")

        to_submit = list(txn_wallet_pairs)
        submitted_count = 0

        async def build_and_submit(txn: Transaction, wallet: Wallet) -> PendingTx | None:
            """Helper to build, sign, and submit a single transaction."""
            try:
                p = await self.build_sign_and_track(txn, wallet)
                await self.submit_pending(p)
                return p
            except Exception as e:
                import traceback

                log.error(
                    f"{label}: Failed to build/submit {txn.transaction_type} for {wallet.address}: "
                    f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
                )
                nonlocal submit_failed
                submit_failed += 1
                return None

        while to_submit:
            pending_by_account: dict[str, int] = {}
            for p in all_pending:
                if p.state in {C.TxState.SUBMITTED, C.TxState.RETRYABLE, C.TxState.FAILED_NET}:
                    pending_by_account[p.account] = pending_by_account.get(p.account, 0) + 1

            max_batch = 22

            batch = []
            remaining = []
            for txn, wallet in to_submit:
                if pending_by_account.get(wallet.address, 0) == 0:
                    if len(batch) < max_batch:
                        batch.append((txn, wallet))
                    else:
                        remaining.append((txn, wallet))
                else:
                    remaining.append((txn, wallet))

            if not batch:
                log.debug(f"{label}: No accounts available ({len(remaining)} waiting), checking finality...")
                for p in all_pending:
                    if p.state not in C.TERMINAL_STATE:
                        try:
                            await self.check_finality(p)
                        except Exception:
                            pass
                await asyncio.sleep(0.5)  # Brief pause before retry
                continue

            async with asyncio.TaskGroup() as tg:
                tasks = [tg.create_task(build_and_submit(txn, wallet)) for txn, wallet in batch]

            batch_pending = [t.result() for t in tasks if t.result() is not None]
            all_pending.extend(batch_pending)
            submitted_count += len(batch_pending)
            to_submit = remaining

            log.info(
                f"  {label}: Submitted {len(batch_pending)} txns ({submitted_count}/{total}, {len(remaining)} waiting, max_batch={max_batch})"
            )

            if to_submit:  # Only wait if there's more to submit
                current_ledger = await self._current_ledger_index()
                for _ in range(20):  # Safety limit
                    new_ledger = await self._current_ledger_index()
                    if new_ledger > current_ledger:
                        break
                    await asyncio.sleep(0.5)

        log.info(f"{label}: All submitted, waiting for finality...")
        for _ in range(200):  # Max iterations as safety
            non_terminal = [p for p in all_pending if p.state not in C.TERMINAL_STATE]
            if not non_terminal:
                break

            for p in non_terminal:
                try:
                    await self.check_finality(p)
                except Exception:
                    pass

            await asyncio.sleep(0.5)

        counts: dict[str, int] = {}
        for p in all_pending:
            counts[p.state.name] = counts.get(p.state.name, 0) + 1
        if submit_failed > 0:
            counts["SUBMIT_FAILED"] = submit_failed

        validated = counts.get("VALIDATED", 0)
        log.info(f"{label} complete: {validated}/{total} validated, {counts}")
        return validated, total, all_pending

    async def init_participants(self, *, gateway_cfg: dict[str, Any], user_cfg: dict[str, Any]) -> dict:
        """Initialize gateways and users with phase-based validation barriers.

        Each phase waits for ALL transactions to validate before proceeding:
        1. Fund gateways -> WAIT -> register gateways
        2. Gateway flags (AccountSet) -> WAIT
        3. Fund users -> WAIT -> register users
        4. TrustSets -> WAIT
        5. Token distribution -> WAIT
        """
        out_gw, out_us = [], []
        req_auth = gateway_cfg["require_auth"]
        def_ripple = gateway_cfg["default_ripple"]
        gateway_names = gateway_cfg.get("names", [])
        currency_codes = self.config["currencies"]["codes"][:4]

        phase_stats: list[dict] = []  # [{name, time_sec, ledgers, validated, total}]
        init_start_time = time.time()
        init_start_ledger = await self._current_ledger_index()

        phase1_start = time.time()
        phase1_ledger_start = await self._current_ledger_index()
        gateway_count = gateway_cfg["number"]
        gateway_balance = str(self.config["gateways"]["default_balance"])
        log.info(f"Phase 1: Funding {gateway_count} gateways")

        gateway_pending: list[tuple[Wallet, PendingTx, int]] = []  # (wallet, pending, index)
        for i in range(gateway_count):
            w = Wallet.create()
            fund_tx = Payment(
                account=self.funding_wallet.address,
                destination=w.address,
                amount=gateway_balance,
            )
            p = await self.build_sign_and_track(fund_tx, self.funding_wallet)
            await self.submit_pending(p)
            gateway_pending.append((w, p, i))

        gw_results = await self._wait_all_validated([p for _, p, _ in gateway_pending], "Phase 1: Gateway funding")

        for w, p, i in gateway_pending:
            if p.state == C.TxState.VALIDATED:
                self.wallets[w.address] = w
                self._record_for(w.address)
                self.gateways.append(w)
                if i < len(gateway_names):
                    self.gateway_names[w.address] = gateway_names[i]
                self.save_wallet_to_store(w, is_gateway=True)
                out_gw.append(w.address)
            else:
                log.error(f"Gateway funding failed: {w.address} state={p.state.name}")

        if not self.gateways:
            raise RuntimeError("No gateways funded successfully, cannot continue")

        for gateway in self.gateways:
            gateway_currencies = issue_currencies(gateway.address, currency_codes)
            self._currencies.extend(gateway_currencies)
            gw_name = self.get_account_display_name(gateway.address)
            log.debug(f"Gateway {gw_name} will issue: {currency_codes}")

        self.update_txn_context()

        phase1_ledger_end = await self._current_ledger_index()
        phase_stats.append(
            {
                "name": "Phase 1: Gateway funding",
                "time_sec": round(time.time() - phase1_start, 2),
                "ledgers": phase1_ledger_end - phase1_ledger_start,
                "validated": gw_results.get("VALIDATED", 0),
                "total": gateway_count,
            }
        )

        phase2_start = time.time()
        phase2_ledger_start = await self._current_ledger_index()
        if req_auth or def_ripple:
            log.info(f"Phase 2: Setting gateway flags (require_auth={req_auth}, default_ripple={def_ripple})")

            flag_pending: list[PendingTx] = []
            flags_to_set: list[AccountSetAsfFlag] = []
            if req_auth:
                flags_to_set.append(AccountSetAsfFlag.ASF_REQUIRE_AUTH)
            if def_ripple:
                flags_to_set.append(AccountSetAsfFlag.ASF_DEFAULT_RIPPLE)

            for w in self.gateways:
                for flag in flags_to_set:
                    tx = AccountSet(account=w.address, set_flag=flag)
                    p = await self.build_sign_and_track(tx, w)
                    await self.submit_pending(p)
                    flag_pending.append(p)

            p2_results = await self._wait_all_validated(flag_pending, "Phase 2: Gateway flags")
            phase2_validated = p2_results.get("VALIDATED", 0)
            phase2_total = len(flag_pending)
        else:
            log.info("Phase 2: Skipping gateway flags (none configured)")
            phase2_validated = 0
            phase2_total = 0

        phase2_ledger_end = await self._current_ledger_index()
        phase_stats.append(
            {
                "name": "Phase 2: Gateway flags",
                "time_sec": round(time.time() - phase2_start, 2),
                "ledgers": phase2_ledger_end - phase2_ledger_start,
                "validated": phase2_validated,
                "total": phase2_total,
            }
        )

        phase3_start = time.time()
        phase3_ledger_start = await self._current_ledger_index()
        user_count = user_cfg["number"]
        user_balance = str(self.config["users"]["default_balance"])
        batch_size = 10  # Funding wallet can have max 10 in queue
        log.info(f"Phase 3: Funding {user_count} users in batches of {batch_size}")

        new_wallets = [Wallet.create() for _ in range(user_count)]
        user_pending: list[tuple[Wallet, PendingTx]] = []

        for batch_start in range(0, user_count, batch_size):
            batch_wallets = new_wallets[batch_start : batch_start + batch_size]
            batch_pending: list[tuple[Wallet, PendingTx]] = []

            for w in batch_wallets:
                fund_tx = Payment(
                    account=self.funding_wallet.address,
                    destination=w.address,
                    amount=user_balance,
                )
                p = await self.build_sign_and_track(fund_tx, self.funding_wallet)
                await self.submit_pending(p)
                batch_pending.append((w, p))

            log.info(f"  Phase 3: Submitted batch {batch_start // batch_size + 1} ({len(batch_pending)} users)")

            for _ in range(60):
                for w, p in batch_pending:
                    if p.state not in C.TERMINAL_STATE:
                        try:
                            await self.check_finality(p)
                        except Exception:
                            pass
                if all(p.state in C.TERMINAL_STATE for _, p in batch_pending):
                    break
                await asyncio.sleep(0.5)

            user_pending.extend(batch_pending)

        for w, p in user_pending:
            if p.state == C.TxState.VALIDATED:
                self.wallets[w.address] = w
                self._record_for(w.address)
                self.users.append(w)
                self.save_wallet_to_store(w, is_user=True)
                out_us.append(w.address)
            else:
                log.error(f"User funding failed: {w.address} state={p.state.name}")

        validated_users = len(self.users)
        log.info(f"Phase 3 complete: {validated_users}/{user_count} users funded")

        if not self.users:
            raise RuntimeError("No users funded successfully, cannot continue")

        self.update_txn_context()

        phase3_ledger_end = await self._current_ledger_index()
        phase_stats.append(
            {
                "name": "Phase 3: User funding",
                "time_sec": round(time.time() - phase3_start, 2),
                "ledgers": phase3_ledger_end - phase3_ledger_start,
                "validated": validated_users,
                "total": user_count,
            }
        )

        phase4_start = time.time()
        phase4_ledger_start = await self._current_ledger_index()
        total_trustsets = len(self.users) * len(self._currencies)
        log.info(
            f"Phase 4: Establishing {total_trustsets} trust lines ({len(self.users)} users × {len(self._currencies)} currencies)"
        )

        trust_limit = str(self.config["transactions"]["trustset"]["limit"])

        trust_txn_pairs: list[tuple[Transaction, Wallet]] = []
        for currency in self._currencies:
            for user in self.users:
                trust_tx = TrustSet(
                    account=user.address,
                    limit_amount=IssuedCurrencyAmount(
                        currency=currency.currency,
                        issuer=currency.issuer,
                        value=trust_limit,
                    ),
                )
                trust_txn_pairs.append((trust_tx, user))

        phase4_validated, phase4_total, _ = await self._init_batch(trust_txn_pairs, "Phase 4: TrustSets")
        if phase4_validated < phase4_total:
            log.warning(f"Phase 4: Only {phase4_validated}/{phase4_total} TrustSets validated - some tokens may fail")

        phase4_ledger_end = await self._current_ledger_index()
        phase_stats.append(
            {
                "name": "Phase 4: TrustSets",
                "time_sec": round(time.time() - phase4_start, 2),
                "ledgers": phase4_ledger_end - phase4_ledger_start,
                "validated": phase4_validated,
                "total": phase4_total,
            }
        )

        phase5_start = time.time()
        phase5_ledger_start = await self._current_ledger_index()
        import math

        num_users = len(self.users)
        num_gateways = len(self.gateways)
        seed_count = min(max(int(math.sqrt(num_users)), num_gateways * 2), num_users)
        seed_users = self.users[:seed_count]

        base_amount = int(self.config.get("currencies", {}).get("token_distribution", 1_000_000))
        users_per_seed = max(1, num_users // seed_count)
        seed_amount = base_amount * users_per_seed * 2  # 2x buffer for redistribution
        seed_amount_str = str(seed_amount)

        total_payments = seed_count * len(self._currencies)
        log.info(f"Phase 5: Seeding {seed_count} users with tokens ({total_payments} payments)")
        log.info(f"  Formula: max(sqrt({num_users}), {num_gateways}*2) = {seed_count} seeds")
        log.info(f"  Amount per seed: {seed_amount} ({users_per_seed} users/seed × {base_amount} × 2)")
        log.info(f"  Remaining {num_users - seed_count} users receive tokens via fan-out during continuous workload")

        dist_txn_pairs: list[tuple[Transaction, Wallet]] = []
        balance_updates: list[tuple[str, str, float, str]] = []  # (user, currency, amount, issuer)

        for user in seed_users:
            for currency in self._currencies:
                issuer_wallet = self.wallets.get(currency.issuer)
                if not issuer_wallet:
                    log.error(f"Cannot find wallet for gateway {currency.issuer}")
                    continue

                payment_tx = Payment(
                    account=currency.issuer,
                    destination=user.address,
                    amount=IssuedCurrencyAmount(
                        currency=currency.currency,
                        issuer=currency.issuer,
                        value=seed_amount_str,
                    ),
                )
                dist_txn_pairs.append((payment_tx, issuer_wallet))
                balance_updates.append((user.address, currency.currency, float(seed_amount), currency.issuer))

        phase5_validated, phase5_total, phase5_pending = await self._init_batch(
            dist_txn_pairs, "Phase 5: Token seeding"
        )

        log.info(f"Phase 5: Waiting for all {phase5_total} txns to reach terminal state...")
        start_ledger = await self._current_ledger_index()

        max_wait_ledgers = 20  # Safety: 20 ledgers (~70s at 3.5s/ledger)
        last_log_ledger = start_ledger

        while True:
            current_ledger = await self._current_ledger_index()

            terminal_count = sum(1 for p in phase5_pending if p.state in C.TERMINAL_STATE)
            pending_count = phase5_total - terminal_count

            if current_ledger >= last_log_ledger + 5:
                log.info(
                    f"  Phase 5: {terminal_count}/{phase5_total} resolved, {pending_count} pending @ ledger {current_ledger}"
                )
                last_log_ledger = current_ledger

            if terminal_count == phase5_total:
                log.info(f"Phase 5: All {phase5_total} txns resolved @ ledger {current_ledger}")
                break

            if current_ledger >= start_ledger + max_wait_ledgers:
                log.warning(f"Phase 5: Timeout after {max_wait_ledgers} ledgers - {pending_count} still pending")
                break

            await asyncio.sleep(1)

        phase5_counts: dict[str, int] = {}
        for p in phase5_pending:
            phase5_counts[p.state.name] = phase5_counts.get(p.state.name, 0) + 1
        phase5_validated = phase5_counts.get("VALIDATED", 0)

        log.info(f"Phase 5: Final results: {phase5_validated}/{phase5_total} validated, {phase5_counts}")

        for user_addr, currency_code, amount, issuer in balance_updates:
            self._set_balance(user_addr, currency_code, amount, issuer)

        if phase5_validated < phase5_total:
            log.warning(f"Phase 5: Only {phase5_validated}/{phase5_total} token distributions validated")

        phase5_ledger_end = await self._current_ledger_index()
        phase_stats.append(
            {
                "name": "Phase 5: Token seeding",
                "time_sec": round(time.time() - phase5_start, 2),
                "ledgers": phase5_ledger_end - phase5_ledger_start,
                "validated": phase5_validated,
                "total": phase5_total,
            }
        )

        for user in seed_users:
            if user.address not in self.user_token_status:
                self.user_token_status[user.address] = set()
            for currency in self._currencies:
                self.user_token_status[user.address].add((currency.currency, currency.issuer))

        phase6_start = time.time()
        phase6_ledger_start = await self._current_ledger_index()

        unfunded_users = [u for u in self.users if u not in seed_users]
        log.info(f"Phase 6: Fan-out distribution to {len(unfunded_users)} remaining users")

        fanout_txn_pairs: list[tuple[Transaction, Wallet]] = []
        fanout_amount_str = str(base_amount)  # 1M per user (final balance)

        users_per_seed_fanout = len(unfunded_users) // len(seed_users)
        extra_users = len(unfunded_users) % len(seed_users)

        user_idx = 0
        for seed_idx, seed_user in enumerate(seed_users):
            num_assigned = users_per_seed_fanout + (1 if seed_idx < extra_users else 0)
            assigned_users = unfunded_users[user_idx : user_idx + num_assigned]
            user_idx += num_assigned

            for currency in self._currencies:
                for target_user in assigned_users:
                    payment_tx = Payment(
                        account=seed_user.address,
                        destination=target_user.address,
                        amount=IssuedCurrencyAmount(
                            currency=currency.currency,
                            issuer=currency.issuer,
                            value=fanout_amount_str,
                        ),
                    )
                    fanout_txn_pairs.append((payment_tx, seed_user))

        total_fanout = len(fanout_txn_pairs)
        log.info(
            f"  {len(unfunded_users)} users × {len(self._currencies)} currencies = {total_fanout} fan-out payments"
        )

        phase6_validated, phase6_total, phase6_pending = await self._init_batch(fanout_txn_pairs, "Phase 6: Fan-out")

        log.info(f"Phase 6: Waiting for all {phase6_total} txns to reach terminal state...")
        start_ledger = await self._current_ledger_index()

        max_wait_ledgers = 30  # Safety: 30 ledgers (~105s) - more txns than Phase 5
        last_log_ledger = start_ledger

        while True:
            current_ledger = await self._current_ledger_index()

            terminal_count = sum(1 for p in phase6_pending if p.state in C.TERMINAL_STATE)
            pending_count = phase6_total - terminal_count

            if current_ledger >= last_log_ledger + 5:
                log.info(
                    f"  Phase 6: {terminal_count}/{phase6_total} resolved, {pending_count} pending @ ledger {current_ledger}"
                )
                last_log_ledger = current_ledger

            if terminal_count == phase6_total:
                log.info(f"Phase 6: All {phase6_total} txns resolved @ ledger {current_ledger}")
                break

            if current_ledger >= start_ledger + max_wait_ledgers:
                log.warning(f"Phase 6: Timeout after {max_wait_ledgers} ledgers - {pending_count} still pending")
                break

            await asyncio.sleep(1)

        phase6_counts: dict[str, int] = {}
        for p in phase6_pending:
            phase6_counts[p.state.name] = phase6_counts.get(p.state.name, 0) + 1
        phase6_validated = phase6_counts.get("VALIDATED", 0)

        log.info(f"Phase 6: Final results: {phase6_validated}/{phase6_total} validated, {phase6_counts}")

        for user in unfunded_users:
            if user.address not in self.user_token_status:
                self.user_token_status[user.address] = set()
            for currency in self._currencies:
                self.user_token_status[user.address].add((currency.currency, currency.issuer))

        phase6_ledger_end = await self._current_ledger_index()
        phase_stats.append(
            {
                "name": "Phase 6: Fan-out",
                "time_sec": round(time.time() - phase6_start, 2),
                "ledgers": phase6_ledger_end - phase6_ledger_start,
                "validated": phase6_validated,
                "total": phase6_total,
            }
        )

        funded_users = len(self.user_token_status)
        total_expected = len(self.users) * len(self._currencies)
        total_received = sum(len(tokens) for tokens in self.user_token_status.values())
        log.info(
            f"Token distribution complete: {funded_users}/{len(self.users)} users, {total_received}/{total_expected} currency pairs"
        )

        self.save_currencies_to_store()

        # Phase 7: Gateway AMM Creation (XRP/IOU pools)
        phase7_start = time.time()
        phase7_ledger_start = await self._current_ledger_index()

        amm_cfg = self.config.get("amm", {})
        gateway_pool_count = amm_cfg.get("gateway_pools", 12)
        pools_per_gateway = max(1, gateway_pool_count // len(self.gateways))

        log.info(f"Phase 7: Creating {gateway_pool_count} gateway AMM pools ({pools_per_gateway} per gateway)")

        amm_txn_pairs: list[tuple[Transaction, Wallet]] = []
        pool_count = 0

        for gw in self.gateways:
            gw_currencies = [c for c in self._currencies if c.issuer == gw.address]
            pools_for_gw = gw_currencies[:pools_per_gateway]

            for currency in pools_for_gw:
                if pool_count >= gateway_pool_count:
                    break

                xrp_amount = amm_cfg.get("deposit_amount_xrp", "1000000000")
                iou_amount = IssuedCurrencyAmount(
                    currency=currency.currency,
                    issuer=currency.issuer,
                    value=str(amm_cfg.get("default_amm_token_deposit", 1000)),
                )

                amm_create_tx = AMMCreate(
                    account=gw.address,
                    amount=xrp_amount,
                    amount2=iou_amount,
                    trading_fee=amm_cfg.get("trading_fee", 500),
                )
                amm_txn_pairs.append((amm_create_tx, gw))
                pool_count += 1

        phase7_validated, phase7_total, phase7_pending = await self._init_batch(amm_txn_pairs, "Phase 7: Gateway AMMs")

        for p in phase7_pending:
            if p.state == C.TxState.VALIDATED and p.meta_txn_result == "tesSUCCESS":
                tx_json = p.tx_json
                if tx_json:
                    amount1 = tx_json.get("Amount")
                    amount2 = tx_json.get("Amount2")
                    if amount1 and amount2:
                        asset1 = (
                            {"currency": "XRP"}
                            if isinstance(amount1, str)
                            else {"currency": amount1["currency"], "issuer": amount1["issuer"]}
                        )
                        asset2 = (
                            {"currency": "XRP"}
                            if isinstance(amount2, str)
                            else {"currency": amount2["currency"], "issuer": amount2["issuer"]}
                        )
                        self._register_amm_pool(asset1, asset2, p.account)

        phase7_ledger_end = await self._current_ledger_index()
        phase_stats.append(
            {
                "name": "Phase 7: Gateway AMMs",
                "time_sec": round(time.time() - phase7_start, 2),
                "ledgers": phase7_ledger_end - phase7_ledger_start,
                "validated": phase7_validated,
                "total": phase7_total,
            }
        )

        # Phase 8: User AMM Creation (IOU/IOU pools)
        phase8_start = time.time()
        phase8_ledger_start = await self._current_ledger_index()

        user_pool_count = amm_cfg.get("user_pools", 100)
        log.info(f"Phase 8: Creating {user_pool_count} user AMM pools (IOU/IOU)")

        from itertools import combinations
        from random import shuffle

        all_iou_pairs = list(combinations(self._currencies, 2))
        all_iou_pairs = [
            (c1, c2) for c1, c2 in all_iou_pairs if not (c1.currency == c2.currency and c1.issuer == c2.issuer)
        ]
        shuffle(all_iou_pairs)

        user_amm_txn_pairs: list[tuple[Transaction, Wallet]] = []
        created_pairs: set[frozenset[str]] = set(self.amm.pool_ids)
        user_idx = 0

        for c1, c2 in all_iou_pairs:
            if len(user_amm_txn_pairs) >= user_pool_count:
                break

            pair_key = frozenset([f"{c1.currency}.{c1.issuer}", f"{c2.currency}.{c2.issuer}"])
            if pair_key in created_pairs:
                continue
            created_pairs.add(pair_key)

            omit = [c1.issuer, c2.issuer]
            available = [u for u in self.users if u.address not in omit]
            if not available:
                continue
            user = available[user_idx % len(available)]
            user_idx += 1

            amm_create_tx = AMMCreate(
                account=user.address,
                amount=IssuedCurrencyAmount(
                    currency=c1.currency,
                    issuer=c1.issuer,
                    value=str(amm_cfg.get("default_amm_token_deposit", 1000)),
                ),
                amount2=IssuedCurrencyAmount(
                    currency=c2.currency,
                    issuer=c2.issuer,
                    value=str(amm_cfg.get("default_amm_token_deposit", 1000)),
                ),
                trading_fee=amm_cfg.get("trading_fee", 500),
            )
            user_amm_txn_pairs.append((amm_create_tx, user))

        log.info(
            f"Phase 8: Built {len(user_amm_txn_pairs)} user AMM pool creations (from {len(all_iou_pairs)} possible pairs)"
        )

        phase8_validated, phase8_total, phase8_pending = await self._init_batch(
            user_amm_txn_pairs, "Phase 8: User AMMs"
        )

        for p in phase8_pending:
            if p.state == C.TxState.VALIDATED and p.meta_txn_result == "tesSUCCESS":
                tx_json = p.tx_json
                if tx_json:
                    amount1 = tx_json.get("Amount")
                    amount2 = tx_json.get("Amount2")
                    if amount1 and amount2:
                        asset1 = (
                            {"currency": "XRP"}
                            if isinstance(amount1, str)
                            else {"currency": amount1["currency"], "issuer": amount1["issuer"]}
                        )
                        asset2 = (
                            {"currency": "XRP"}
                            if isinstance(amount2, str)
                            else {"currency": amount2["currency"], "issuer": amount2["issuer"]}
                        )
                        self._register_amm_pool(asset1, asset2, p.account)

        phase8_ledger_end = await self._current_ledger_index()
        phase_stats.append(
            {
                "name": "Phase 8: User AMMs",
                "time_sec": round(time.time() - phase8_start, 2),
                "ledgers": phase8_ledger_end - phase8_ledger_start,
                "validated": phase8_validated,
                "total": phase8_total,
            }
        )

        self.update_txn_context()

        init_end_time = time.time()
        init_end_ledger = await self._current_ledger_index()
        total_time = round(init_end_time - init_start_time, 2)
        total_ledgers = init_end_ledger - init_start_ledger
        total_validated = sum(p["validated"] for p in phase_stats)
        total_txns = sum(p["total"] for p in phase_stats)

        log.info("=" * 60)
        log.info("INITIALIZATION SUMMARY")
        log.info("=" * 60)
        for p in phase_stats:
            log.info(
                f"  {p['name']}: {p['validated']}/{p['total']} validated, {p['time_sec']}s, {p['ledgers']} ledgers"
            )
        log.info("-" * 60)
        log.info(f"  TOTAL: {total_validated}/{total_txns} validated, {total_time}s, {total_ledgers} ledgers")
        log.info(f"  Final state: {len(self.gateways)} gateways, {len(self.users)} users at ledger {init_end_ledger}")
        log.info("=" * 60)

        return {"gateways": out_gw, "users": out_us}

    def snapshot_pending(self, *, open_only: bool = True) -> list[dict]:
        out = []
        for txh, p in self.pending.items():
            if open_only and p.state not in C.PENDING_STATES:
                continue
            out.append(
                {
                    "tx_hash": txh,
                    "state": p.state.name,
                    "account": p.account,
                    "sequence": p.sequence,
                    "last_ledger_seq": p.last_ledger_seq,
                    "created_ledger": p.created_ledger,
                    "attempts": p.attempts,
                    "engine_result_first": p.engine_result_first,
                    "engine_result_message": p.engine_result_message,
                    "engine_result_final": p.meta_txn_result or p.engine_result_first,
                    "validated_ledger": p.validated_ledger,
                    "meta_txn_result": p.meta_txn_result,
                    "transaction_type": p.transaction_type,
                }
            )
        return out

    def snapshot_failed(self) -> list[dict[str, Any]]:
        """Return transactions that failed — including tec codes (validated on-chain but failed in intent).

        tec transactions are applied to the ledger (sequence consumed, fee burned) but the
        intended action didn't succeed (e.g., tecUNFUNDED_OFFER). They're VALIDATED in state
        but failures from the user's perspective.
        """
        failed_states = {"REJECTED", "EXPIRED", "FAILED_NET"}
        results = []
        for r in self.snapshot_pending(open_only=False):
            if r.get("engine_result_first") == "CASCADE_EXPIRED":
                continue
            if r["state"] in failed_states:
                results.append(r)
            elif r["state"] == "VALIDATED" and r.get("meta_txn_result", "").startswith("tec"):
                results.append(r)
        return results

    def snapshot_stats(self) -> dict[str, Any]:
        # In-flight counts from pending dict (for "In-Flight" and "Created" cards)
        by_state: dict[str, int] = {}
        for p in self.pending.values():
            state = p.state.name
            by_state[state] = by_state.get(state, 0) + 1

        # Cumulative totals (survive cleanup_terminal)
        by_state["VALIDATED"] = self._total_validated
        by_state["REJECTED"] = self._total_rejected
        by_state["EXPIRED"] = self._total_expired

        uptime = time.time() - self.started_at
        ledgers_elapsed = max(0, self.latest_ledger_index - self.first_ledger_index) if self.first_ledger_index else 0
        result = {
            "total_tracked": self._total_created,
            "by_state": by_state,
            "gateways": len(self.gateways),
            "users": len(self.users),
            "uptime_seconds": round(uptime),
            "started_at": self.started_at,
            "ledger_index": self.latest_ledger_index,
            "first_ledger_index": self.first_ledger_index,
            "ledgers_elapsed": ledgers_elapsed,
        }

        # Merge store-level aggregate stats (submission results, validation sources)
        store_stats = self.store.snapshot_stats()
        for key in ("validated_by_source", "validated_by_result", "submission_results", "recent_validations"):
            if key in store_stats:
                result[key] = store_stats[key]

        # Per-type breakdown: validated / submitted (cumulative counters, survive cleanup)
        result["by_type"] = dict(self.store.count_by_type)
        result["by_type_validated"] = dict(self._type_validated)
        result["by_type_total"] = dict(self._type_submitted)

        result["dex"] = self.snapshot_dex_metrics()

        return result

    def snapshot_dex_metrics(self) -> dict:
        """Return current DEX metrics snapshot for the API."""
        dm = self.dex_metrics
        return {
            "pools_created": dm.pools_created,
            "active_pools": dm.active_pools,
            "total_deposits": dm.total_deposits,
            "total_withdrawals": dm.total_withdrawals,
            "total_offers": dm.total_offers,
            "total_xrp_locked_drops": dm.total_xrp_locked_drops,
            "last_poll_ledger": dm.last_poll_ledger,
            "pool_count": len(self.amm),
            "pool_details": dm.pool_snapshots,
        }

    async def flush_to_persistent_store(self) -> int:
        """Flush in-memory transaction records to the persistent store (SQLite).

        Called on shutdown to ensure durability. Uses a single bulk upsert
        instead of one connection per record.
        """
        from workload.sqlite_store import SQLiteStore

        if not isinstance(self.persistent_store, SQLiteStore):
            return 0

        records = [
            (
                tx_hash,
                {
                    "state": p.state,
                    "account": p.account,
                    "sequence": p.sequence,
                    "transaction_type": p.transaction_type,
                    "engine_result_first": p.engine_result_first,
                    "validated_ledger": p.validated_ledger,
                    "meta_txn_result": p.meta_txn_result,
                    "finalized_at": p.finalized_at,
                },
            )
            for tx_hash, p in self.pending.items()
        ]

        if not records:
            return 0

        try:
            flushed = await self.persistent_store.bulk_upsert(records)
            log.info("Flushed %d records to persistent store", flushed)
            return flushed
        except Exception as e:
            log.warning("Bulk flush failed: %s", e)
            return 0

    async def poll_dex_metrics(self) -> dict:
        """Poll amm_info for all tracked AMM pools and update DEX metrics."""
        from xrpl.models.currencies import XRP as XRPCurrency
        from xrpl.models.requests import AMMInfo

        pool_snapshots = []
        total_xrp_locked = 0

        for pool in self.amm.pools:
            try:
                asset1 = pool["asset1"]
                asset2 = pool["asset2"]

                if asset1.get("currency") == "XRP":
                    a1 = XRPCurrency()
                else:
                    a1 = IssuedCurrency(currency=asset1["currency"], issuer=asset1["issuer"])

                if asset2.get("currency") == "XRP":
                    a2 = XRPCurrency()
                else:
                    a2 = IssuedCurrency(currency=asset2["currency"], issuer=asset2["issuer"])

                resp = await self._rpc(AMMInfo(asset=a1, asset2=a2), t=3.0)
                if resp.is_successful():
                    amm_data = resp.result.get("amm", {})
                    snapshot = {
                        "asset1": asset1,
                        "asset2": asset2,
                        "amount": amm_data.get("amount"),
                        "amount2": amm_data.get("amount2"),
                        "lp_token": amm_data.get("lp_token"),
                        "trading_fee": amm_data.get("trading_fee"),
                        "vote_slots": len(amm_data.get("vote_slots", [])),
                    }
                    pool_snapshots.append(snapshot)

                    amt = amm_data.get("amount")
                    if isinstance(amt, str):
                        total_xrp_locked += int(amt)
                    amt2 = amm_data.get("amount2")
                    if isinstance(amt2, str):
                        total_xrp_locked += int(amt2)

            except Exception as e:
                log.debug(f"Failed to poll amm_info for pool: {e}")

        current_ledger = await self._current_ledger_index()
        self.dex_metrics.pool_snapshots = pool_snapshots
        self.dex_metrics.last_poll_ledger = current_ledger
        self.dex_metrics.total_xrp_locked_drops = total_xrp_locked
        self.dex_metrics.active_pools = len(pool_snapshots)

        return self.dex_metrics

    def snapshot_tx(self, tx_hash: str) -> dict[str, Any]:
        p = self.pending.get(tx_hash)
        ws_port = 6006  # TODO: Use the real ws port
        if not p:
            return {}
        return {
            "tx_hash": p.tx_hash,
            "state": p.state.name,
            "account": p.account,
            "sequence": p.sequence,
            "last_ledger_seq": p.last_ledger_seq,
            "created_ledger": p.created_ledger,
            "attempts": p.attempts,
            "engine_result_first": p.engine_result_first,
            "validated_ledger": p.validated_ledger,
            "meta_txn_result": p.meta_txn_result,
            "link": f"https://custom.xrpl.org/localhost:{ws_port}/transactions/{tx_hash}",
        }

    def _cleanup_terminal(self, keep_recent: int = 200) -> int:
        """Remove old terminal txns from self.pending (already persisted in store)."""
        terminal = [(txh, p) for txh, p in self.pending.items() if p.state in C.TERMINAL_STATE]
        if len(terminal) <= keep_recent:
            return 0
        terminal.sort(key=lambda x: x[1].finalized_at or x[1].created_at)
        to_remove = terminal[:-keep_recent]
        for txh, _ in to_remove:
            del self.pending[txh]
        return len(to_remove)


async def periodic_dex_metrics(w: Workload, stop: asyncio.Event, poll_interval_ledgers: int = 5):
    """Periodically poll DEX metrics every N ledgers."""
    last_polled_ledger = 0
    while not stop.is_set():
        try:
            if not w.amm:
                await asyncio.sleep(5)
                continue

            current = await w._current_ledger_index()
            if current >= last_polled_ledger + poll_interval_ledgers:
                await w.poll_dex_metrics()
                last_polled_ledger = current
                log.debug("DEX metrics polled at ledger %d (%d pools)", current, len(w.amm))

            await asyncio.sleep(3)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("[dex_metrics] poll error; continuing")
            await asyncio.sleep(5)


async def periodic_finality_check(w: Workload, stop: asyncio.Event, interval: int = 5):
    iteration = 0
    while not stop.is_set():
        try:
            for p in w.find_by_state(C.TxState.SUBMITTED, C.TxState.RETRYABLE, C.TxState.FAILED_NET):
                try:
                    await w.check_finality(p)
                except Exception:
                    log.exception("[finality] check failed for %s", getattr(p, "tx_hash", p))

            iteration += 1
            if iteration % 10 == 0:
                removed = w._cleanup_terminal()
                if removed:
                    log.debug("[finality] cleaned up %d terminal txns from pending", removed)

            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("[finality] outer loop error; continuing")
            await asyncio.sleep(0.5)

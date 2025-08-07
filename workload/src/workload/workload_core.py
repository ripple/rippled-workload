import asyncio
import hashlib
import json
import logging
import multiprocessing
import sys
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from enum import StrEnum, auto
from typing import Any, Protocol

import httpx
import xrpl
from xrpl.asyncio.clients import AsyncJsonRpcClient
from xrpl.asyncio.ledger import get_latest_validated_ledger_sequence
from xrpl.core.binarycodec import encode, encode_for_signing
from xrpl.core.keypairs import sign
from xrpl.models import IssuedCurrency, SubmitOnly, Transaction
from xrpl.models.amounts import IssuedCurrencyAmount
from xrpl.models.requests import (
    AccountInfo,
    AccountLines,
    AccountObjects,
    Ledger,
    ServerState,
    Tx,
)
from xrpl.models.transactions import (
    AccountSet,
    AccountSetAsfFlag,
    Payment,
    TrustSet,
)
from xrpl.wallet import Wallet

import workload.constants as C
from workload.txn_factory.builder import TxnContext, generate_txn

num_cpus = multiprocessing.cpu_count()

log = logging.getLogger("workload.core")


logging.basicConfig(level=logging.INFO, stream=sys.stdout, format="%(asctime)s %(levelname)s %(message)s")

log = logging.getLogger("workload")

TERMINAL_STATE = {C.TxState.VALIDATED, C.TxState.REJECTED, C.TxState.EXPIRED}


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
    validated_ledger: int | None = None
    meta_txn_result: str | None = None
    created_at: float = field(default_factory=time.time)
    finalized_at: float | None = None

    def __str__(self):
        return f"{self.transaction_type} -- {self.account} -- {self.state}"


class Store(Protocol):
    async def upsert(self, p: PendingTx) -> None: ...
    async def get(self, tx_hash: str) -> PendingTx | None: ...
    async def mark(self, tx_hash: str, **fields) -> None: ...
    async def rekey(self, old_hash: str, new_hash: str) -> None: ...
    async def find_by_state(self, *states: C.TxState) -> list[PendingTx]: ...
    async def all(self) -> list[PendingTx]: ...


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
        self.count_by_state = Counter(rec.get("state", "UNKNOWN") for rec in self._records.values())
        self.count_by_type = Counter(rec.get("transaction_type", "UNKNOWN") for rec in self._records.values())
        self.validated_by_source = Counter(v.src for v in self.validations)

    async def update_record(self, tx: dict) -> None:
        """Insert or update a flat transaction record and recompute metrics."""
        txh = tx.get("tx_hash")
        log.debug("update_record %s", txh)
        if not txh:
            raise ValueError("update_record() requires 'tx_hash'")
        async with self._lock:
            self._records[txh] = tx
            self._recount()

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
        log.debug("Mark %s", tx_hash)
        async with self._lock:
            rec = self._records.get(tx_hash, {})

            prev_state = rec.get("state")
            rec_before = dict(rec)
            rec.update(fields)
            rec_after = dict(rec)

            if set(rec_after.items()) - set(rec_before.items()):
                d = set(rec_after.items()) - set(rec_before.items())
                log.debug("After has more diff %s", d)
            elif set(rec_before.items()) - set(rec_after.items()):
                d = set(rec_before.items()) - set(rec_after.items())
                log.debug("Before has more diff %s", d)

            if source is not None:
                rec["source"] = source

            state = rec.get("state")
            if isinstance(state, C.TxState):  # normalize enum to string
                state = state.name
                rec["state"] = state

            if state in TERMINAL_STATE:
                rec.setdefault("finalized_at", time.time())

                if state == "VALIDATED" and prev_state != "VALIDATED":
                    seq = rec.get("validated_ledger") or 0
                    src = source or rec.get("source", "unknown")
                    if not any(v.txn == tx_hash and v.seq == seq for v in self.validations):
                        log.debug("%s ValidationRecord for in %s by %s -- %s", state, seq, src, tx_hash)
                        self.validations.append(ValidationRecord(txn=tx_hash, seq=seq, src=src))

            self._records[tx_hash] = rec
            self._recount()
            log.debug("%s --> %s  %s", prev_state, state, tx_hash)

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


class ValidationSrc(StrEnum):
    POLL = auto()
    WS = auto()


@dataclass
class ValidationRecord:
    txn: str
    seq: int
    src: str


def _sha512half(b: bytes) -> bytes:
    return hashlib.sha512(b).digest()[:32]


def _txid_from_signed_blob_hex(signed_blob_hex: str) -> str:
    return _sha512half(bytes.fromhex("54584E00") + bytes.fromhex(signed_blob_hex)).hex().upper()


def issue_currencies(issuer: str, currency_code: list[str]) -> list[IssuedCurrency]:
    issued_currencies = [IssuedCurrency.from_dict(dict(issuer=issuer, currency=cc)) for cc in currency_code]
    return issued_currencies


async def debug_last_tx(client: AsyncJsonRpcClient, account: str):
    ai = await client.request(AccountInfo(account=account, ledger_index="validated"))
    try:
        log.debug(
            "acct %s seq=%s bal=%s",
            account,
            ai.result["account_data"]["Sequence"],
            ai.result["account_data"]["Balance"],
        )
    except KeyError as e:
        pass


class Workload:
    def __init__(self, config: dict, client: AsyncJsonRpcClient, *, store: Store | None = None):
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

        self.store: Store = store or InMemoryStore()

        self._fee_cache: int | None = None
        self._fee_lock = asyncio.Lock()

        self.ledger_fill_fraction: float = 0.5

        self.max_pending_per_account: int = self.config.get("transactions", {}).get("max_pending_per_account", 10)

        self.target_txns_per_ledger: int = 30

        self.user_token_status: dict[str, set[tuple[str, str]]] = {}

        self._currencies: list[IssuedCurrency] = []

        self._mptoken_issuance_ids: list[str] = []

        self.balances: dict[str, dict[str | tuple[str, str], float]] = {}

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
        ctx.balances = self.balances
        return ctx

    def update_txn_context(self):
        self.ctx = self.configure_txn_context(
            wallets=list(self.wallets.values()),
            funding_wallet=self.funding_wallet,
        )

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

        if not isinstance(self.store, SQLiteStore):
            log.debug("Store is not SQLiteStore, cannot load state")
            return False

        if not self.store.has_state():
            log.debug("No persisted state found in database")
            return False

        log.debug("Loading workload state from database...")

        self.pending.clear()

        gateway_names_from_config = self.config.get("gateways", {}).get("names", [])
        wallet_data = self.store.load_wallets()
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

        currencies = self.store.load_currencies()
        self._currencies = currencies

        log.debug(
            f"Loaded state: {len(self.wallets)} wallets "
            f"({len(self.gateways)} gateways, {len(self.users)} users), "
            f"{len(self._currencies)} currencies"
        )

        if len(self.gateways) > 0 and len(self._currencies) == 0:
            log.debug("Incomplete state detected: gateways exist but no currencies found. Rejecting loaded state.")
            self.wallets.clear()
            self.gateways.clear()
            self.users.clear()
            self.gateway_names.clear()
            self._currencies = []
            return False

        self.update_txn_context()

        return True

    def save_wallet_to_store(
        self, wallet: Wallet, is_gateway: bool = False, is_user: bool = False, funded_ledger_index: int | None = None
    ) -> None:
        """Save a wallet to the persistent store."""
        from workload.sqlite_store import SQLiteStore

        if isinstance(self.store, SQLiteStore):
            self.store.save_wallet(
                wallet, is_gateway=is_gateway, is_user=is_user, funded_ledger_index=funded_ledger_index
            )

    def save_currencies_to_store(self) -> None:
        """Save all currencies to the persistent store."""
        from workload.sqlite_store import SQLiteStore

        if isinstance(self.store, SQLiteStore):
            for currency in self._currencies:
                self.store.save_currency(currency)

    async def _latest_validated_ledger(self) -> int:
        return await get_latest_validated_ledger_sequence(client=self.client)

    def _record_for(self, addr: str) -> AccountRecord:
        rec = self.accounts.get(addr)
        if rec is None:
            log.debug("_record for %s", addr)
            rec = AccountRecord(lock=asyncio.Lock(), next_seq=None)
            self.accounts[addr] = rec
        return rec

    def _get_balance(self, account: str, currency: str, issuer: str | None = None) -> float:
        """Get tracked balance for an account."""
        if account not in self.balances:
            return 0.0
        if currency == "XRP":
            return self.balances[account].get("XRP", 0.0)
        else:
            key = (currency, issuer) if issuer else currency
            return self.balances[account].get(key, 0.0)

    def _set_balance(self, account: str, currency: str, value: float, issuer: str | None = None):
        """Set tracked balance for an account."""
        if account not in self.balances:
            self.balances[account] = {}
        if currency == "XRP":
            self.balances[account]["XRP"] = value
        else:
            key = (currency, issuer) if issuer else currency
            self.balances[account][key] = value

    def _update_balance(self, account: str, currency: str, delta: float, issuer: str | None = None):
        """Update (credit/debit) tracked balance for an account."""
        current = self._get_balance(account, currency, issuer)
        self._set_balance(account, currency, current + delta, issuer)

    async def _rpc(self, req, *, t=C.RPC_TIMEOUT):
        return await asyncio.wait_for(self.client.request(req), timeout=t)

    async def alloc_seq(self, addr: str) -> int:
        rec = self._record_for(addr)

        async with rec.lock:
            if rec.next_seq is None:
                ai = await self._rpc(AccountInfo(account=addr, ledger_index="current", strict=True))
                rec.next_seq = ai.result["account_data"]["Sequence"]

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
            f"💰 Fee: {fee} drops (min={minimum_fee}, open={open_ledger_fee}, base={base_fee}, "
            f"queue={fee_info.current_queue_size}/{fee_info.max_queue_size}, "
            f"ledger={fee_info.current_ledger_size}/{fee_info.expected_ledger_size})"
        )

        if fee > base_fee:
            log.debug(
                f"⚠️  Queue fees escalated: minimum={minimum_fee} (queue), open_ledger={open_ledger_fee} (immediate), base={base_fee}"
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
        log.debug("Creating record %s for %s", p.state, p.tx_hash)
        await self.store.update_record(
            {
                "tx_hash": p.tx_hash,
                "state": p.state,  # or p.state.name?
                "created_ledger": p.created_ledger,
            }
        )

    async def record_submitted(self, p: PendingTx, engine_result: str | None, srv_txid: str | None):
        if p.state in TERMINAL_STATE:
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
        await self.store.mark(
            new_hash,
            state=C.TxState.SUBMITTED,
            account=p.account,
            sequence=p.sequence,
            transaction_type=p.transaction_type,
            engine_result_first=p.engine_result_first,
        )

    async def _update_account_balances(self, account: str) -> None:
        """Fetch and store current balances for an account from the ledger."""
        from workload.sqlite_store import SQLiteStore

        if not isinstance(self.store, SQLiteStore):
            return  # Balance tracking only works with SQLiteStore

        try:
            acc_info = await self._rpc(AccountInfo(account=account, ledger_index="validated"), t=2.0)
            if not acc_info.is_successful():
                log.debug(f"AccountInfo failed for {account}: {acc_info.result}")
                return

            xrp_balance = acc_info.result.get("account_data", {}).get("Balance")
            if xrp_balance:
                self.store.update_balance(account, "XRP", xrp_balance)

            acc_lines = await self._rpc(AccountLines(account=account, ledger_index="validated"), t=2.0)
            if acc_lines.is_successful():
                for line in acc_lines.result.get("lines", []):
                    currency = line.get("currency")
                    issuer = line.get("account")  # The counterparty is the issuer
                    balance = line.get("balance")
                    if currency and issuer and balance:
                        self.store.update_balance(account, "IOU", balance, currency=currency, issuer=issuer)

            acc_objects = await self._rpc(
                AccountObjects(account=account, ledger_index="validated", type="mptoken"), t=2.0
            )
            if acc_objects.is_successful():
                for obj in acc_objects.result.get("account_objects", []):
                    if obj.get("LedgerEntryType") == "MPToken":
                        mpt_id = obj.get("MPTokenIssuanceID")
                        balance = obj.get("MPTAmount", "0")
                        if mpt_id:
                            self.store.update_balance(account, "MPToken", balance, currency=mpt_id, issuer=None)

            log.debug(f"Updated balances for {account}")
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            log.debug(f"Balance update timed out for {account} (expected during heavy load)")
        except Exception as e:
            log.debug(f"Failed to update balances for {account}: {type(e).__name__}: {e}")

    async def record_validated(self, rec: ValidationRecord, meta_result: str | None = None) -> dict:
        p_live = self.pending.get(rec.txn)  # keep this reference

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

        p_live = self.pending.get(rec.txn)
        w = getattr(p_live, "wallet", None)
        if w is not None:
            self.wallets[w.address] = w
            self._record_for(w.address)
            self.users.append(w)
            self.save_wallet_to_store(w, is_user=True, funded_ledger_index=rec.seq)  # Persist with funding ledger
            self.update_txn_context()
            log.debug("Adopted new account after validation: %s", w.address)

        if p_live and meta_result == "tesSUCCESS" and p_live.transaction_type == C.TxType.PAYMENT:
            try:
                tx_json = p_live.tx_json
                if tx_json:
                    sender = tx_json.get("Account")
                    destination = tx_json.get("Destination")
                    amount = tx_json.get("Amount")

                    if sender and destination and amount:
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
                                log.debug(f"Balance update: {sender[:8]} -> {destination[:8]}: {value} {currency}")
            except Exception as e:
                log.debug(f"Failed to update in-memory balances for {rec.txn}: {e}")

        if p_live and p_live.transaction_type == C.TxType.MPTOKEN_ISSUANCE_CREATE:
            try:
                tx_result = await self._rpc(Tx(transaction=rec.txn))
                mpt_id = tx_result.result.get("mpt_issuance_id")
                if mpt_id and mpt_id not in self._mptoken_issuance_ids:
                    self._mptoken_issuance_ids.append(mpt_id)
                    self.update_txn_context()  # Refresh context with new MPToken ID
                    log.debug("Tracked new MPToken issuance ID: %s", mpt_id)
            except Exception as e:
                log.debug(f"Failed to extract MPToken issuance ID from {rec.txn}: {e}")

        if p_live and p_live.transaction_type == C.TxType.BATCH and p_live.account:
            try:
                ai = await self._rpc(AccountInfo(account=p_live.account, ledger_index="current"))
                rec_acct = self._record_for(p_live.account)
                async with rec_acct.lock:
                    old_seq = rec_acct.next_seq
                    rec_acct.next_seq = ai.result["account_data"]["Sequence"]
                    log.debug(f"Batch validated: synced {p_live.account[:8]} sequence {old_seq} -> {rec_acct.next_seq}")
            except Exception as e:
                log.debug(f"Failed to sync sequence after Batch validation: {e}")

        log.debug("txn %s validated at ledger %s via %s", rec.txn, rec.seq, rec.src)
        return {"tx_hash": rec.txn, "ledger_index": rec.seq, "source": rec.src, "meta_result": meta_result}

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
                and p.state not in TERMINAL_STATE
            ):
                p.state = C.TxState.EXPIRED
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

        log.debug(f"Cascade check for {account}: expired {cascade_count} txns with seq > {failed_seq}")

        rec = self._record_for(account)
        async with rec.lock:
            old_seq = rec.next_seq
            if fetch_seq_from_ledger:
                try:
                    ai = await self._rpc(AccountInfo(account=account, ledger_index="current"))
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
                    log.debug(
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

    async def record_expired(self, tx_hash: str):
        if tx_hash not in self.pending:
            return

        p = self.pending[tx_hash]
        p.state = C.TxState.EXPIRED
        await self.store.mark(
            tx_hash,
            state=C.TxState.EXPIRED,
            account=p.account,
            sequence=p.sequence,
            transaction_type=p.transaction_type,
            engine_result_first=p.engine_result_first,
        )

        if p.account and p.sequence is not None:
            if p.transaction_type == C.TxType.BATCH:
                log.debug(
                    f"EXPIRED (Batch): {p.transaction_type} account={p.account} seq={p.sequence} hash={tx_hash} - will cascade and sync from ledger"
                )
            else:
                log.debug(
                    f"EXPIRED: {p.transaction_type} account={p.account} seq={p.sequence} hash={tx_hash} - will cascade and sync from ledger"
                )
            await self._cascade_expire_account(p.account, p.sequence, exclude_hash=tx_hash, fetch_seq_from_ledger=True)

    def find_by_state(self, *states: C.TxState) -> list[PendingTx]:
        return [p for p in self.pending.values() if p.state in set(states)]

    def get_accounts_with_pending_txns(self) -> set[str]:
        """Get set of account addresses that have pending (non-terminal) transactions.

        Returns:
            Set of account addresses with CREATED, SUBMITTED, or RETRYABLE transactions
        """
        PENDING_STATES = {C.TxState.CREATED, C.TxState.SUBMITTED, C.TxState.RETRYABLE}
        accounts = set()
        for p in self.pending.values():
            if p.state in PENDING_STATES and p.account:
                accounts.add(p.account)
        return accounts

    def get_pending_txn_counts_by_account(self) -> dict[str, int]:
        """Get count of pending transactions per account.

        Returns:
            Dict mapping account address to count of CREATED/SUBMITTED/RETRYABLE transactions.
            Used to enforce per-account queue limit of 10 (see FeeEscalation.md:260).
        """
        PENDING_STATES = {C.TxState.CREATED, C.TxState.SUBMITTED, C.TxState.RETRYABLE}
        counts = {}
        for p in self.pending.values():
            if p.state in PENDING_STATES and p.account:
                counts[p.account] = counts.get(p.account, 0) + 1
        return counts

    async def build_sign_and_track(self, txn: Transaction, wallet: Wallet, horizon: int = C.HORIZON) -> PendingTx:
        created_li = (await self._rpc(ServerState(), t=2.0)).result["state"]["validated_ledger"][
            "seq"
        ]  # TODO: Constant
        lls = created_li + horizon
        tx = txn.to_xrpl()
        if tx.get("Flags") == 0:
            del tx["Flags"]

        need_seq = "TicketSequence" not in tx and not tx.get("Sequence")
        need_fee = not tx.get("Fee")

        seq = await self.alloc_seq(wallet.address) if need_seq else tx.get("Sequence")
        base_fee = await self._open_ledger_fee() if need_fee else int(tx["Fee"])

        txn_type = tx.get("TransactionType")
        if txn_type == "Batch":
            OWNER_RESERVE_DROPS = 2_000_000
            inner_txns = tx.get("RawTransactions", [])
            fee = (2 * OWNER_RESERVE_DROPS) + (base_fee * len(inner_txns))
            log.debug(f"Batch fee: 2*{OWNER_RESERVE_DROPS} + {base_fee}*{len(inner_txns)} = {fee} drops")
        elif txn_type == "AMMCreate":
            OWNER_RESERVE_DROPS = 2_000_000
            fee = OWNER_RESERVE_DROPS
            log.debug(f"AMMCreate fee: {fee} drops (owner_reserve)")
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
        )
        await self.record_created(p)
        return p

    async def submit_pending(self, p: PendingTx, timeout: float = C.SUBMIT_TIMEOUT) -> dict | None:
        if p.state in TERMINAL_STATE:
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
            if p.transaction_type == "AccountSet":
                pass
            resp = await asyncio.wait_for(self.client.request(SubmitOnly(tx_blob=p.signed_blob_hex)), timeout=timeout)
            if p.transaction_type == "AccountSet":
                log.debug(resp)
            res = resp.result
            er = res.get("engine_result")
            if p.engine_result_first is None:
                p.engine_result_first = er

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
                log.debug(
                    f"tefPAST_SEQ: {p.transaction_type} account={p.account} seq={p.sequence} hash={p.tx_hash} - will reset from ledger"
                )
                p.state = C.TxState.REJECTED
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
                await self._cascade_expire_account(
                    p.account, p.sequence, exclude_hash=p.tx_hash, fetch_seq_from_ledger=True
                )
                return res

            if isinstance(er, str) and er.startswith(("tem", "tef")):
                p.state = C.TxState.REJECTED
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
                log.debug(f"REJECTED: {er} - {p.transaction_type} from {p.account} seq={p.sequence} hash={p.tx_hash}")

                if p.transaction_type == C.TxType.BATCH and p.account:
                    try:
                        ai = await self._rpc(AccountInfo(account=p.account, ledger_index="current"))
                        rec_acct = self._record_for(p.account)
                        async with rec_acct.lock:
                            old_seq = rec_acct.next_seq
                            rec_acct.next_seq = ai.result["account_data"]["Sequence"]
                            log.debug(
                                f"Batch rejected: synced {p.account[:8]} sequence {old_seq} -> {rec_acct.next_seq}"
                            )
                    except Exception as e:
                        log.debug(f"Failed to sync sequence after Batch rejection: {e}")

                return res

            if er == "terPRE_SEQ":
                log.debug(
                    f"terPRE_SEQ: {p.transaction_type} account={p.account} seq={p.sequence} hash={p.tx_hash} - will cascade expire and reset from ledger"
                )
                p.state = C.TxState.EXPIRED
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

    def log_validation(self, tx_hash, ledger_index, result, validation_src):
        log.debug(
            "Validated via %s tx=%s li=%s result=%s", validation_src, tx_hash, ledger_index, result
        )  # FIX: DEbug only...

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
            log.error("Houston, we have a %s", "major problem", exc_info=True)
            pass

        latest_val = await self._latest_validated_ledger()
        if latest_val > (p.last_ledger_seq + grace):
            await self.record_expired(p.tx_hash)
            return p.state, None

        if p.state != C.TxState.SUBMITTED:
            p.state = C.TxState.RETRYABLE
            await self.store.mark(p.tx_hash, state=p.state)
        return p.state, None

    async def submit_signed_tx_blobs(self, items: list):
        def _to_blob(x):
            if isinstance(x, str):
                return x
            if isinstance(x, (tuple, list)):
                return x[0]
            blob = getattr(x, "signed_blob_hex", None)
            if isinstance(blob, str):
                return blob
            raise TypeError(f"unsupported tx item: {type(x)}")

        blobs = [_to_blob(i) for i in items]
        async with asyncio.TaskGroup() as tg:
            tasks = [tg.create_task(self.client.request(SubmitOnly(tx_blob=b))) for b in blobs]
        return [t.result().result for t in tasks]

    async def _is_account_active(self, address: str) -> bool:
        try:
            r = await self.client.request(AccountInfo(account=address, ledger_index="validated"))
            return r.is_successful()
        except Exception:
            return False  # TODO: Say something?

    async def _ensure_funded(self, wallet: Wallet, amt_drops: str):
        """Fund a wallet from the workload funding_wallet account if it hasn't been created yet."""
        if await self._is_account_active(wallet.address):
            return
        amt_drops = str(amt_drops)
        fund_tx = Payment(
            account=self.funding_wallet.address,
            destination=wallet.address,
            amount=amt_drops,
        )

        p = await self.build_sign_and_track(fund_tx, self.funding_wallet)

        await debug_last_tx(self.client, p.account)
        await self.submit_pending(p)
        log.debug(f"Funded {wallet.address} with {int(xrpl.utils.drops_to_xrp(amt_drops))} XRP")
        await debug_last_tx(self.client, p.account)

    async def _acctset_flags(self, wallet: Wallet, *, require_auth=False, default_ripple=True):
        flags = []
        if require_auth:
            flags.append(AccountSetAsfFlag.ASF_REQUIRE_AUTH)
        if default_ripple:
            flags.append(AccountSetAsfFlag.ASF_DEFAULT_RIPPLE)
        for f in flags:
            t = AccountSet(account=wallet.address, set_flag=f)
            p = await self.build_sign_and_track(t, wallet)
            log.debug("Submitting AccountSet")
            await self.submit_pending(p)
            log.debug("Submitted AccountSet %s", p.tx_json)
            log.debug(json.dumps(p.tx_json))

    async def wait_for_validation(self, tx_hash: str, *, overall: float = 15.0, per_rpc: float = 2.0) -> dict:
        from xrpl.models.requests import Tx

        try:
            async with asyncio.timeout(overall):
                while True:
                    r = await asyncio.wait_for(self.client.request(Tx(transaction=tx_hash)), timeout=per_rpc)
                    if r.result.get("validated"):
                        return r.result
                    await asyncio.sleep(0.5)
        except TimeoutError:
            return {"validated": False, "timeout": True}

    async def bootstrap_gateway(self, w, *, drops=1_000_000_000, require_auth=False, default_ripple=False):
        fund = Payment(account=w.address, destination=w.address, amount=str(drops))  # or from funder→w
        p0 = await self.build_sign_and_track(fund, self.funding_wallet)
        await self.submit_pending(p0)

        flags = []
        if require_auth:
            flags.append(AccountSetAsfFlag.ASF_REQUIRE_AUTH)
        if default_ripple:
            flags.append(AccountSetAsfFlag.ASF_DEFAULT_RIPPLE)

        pendings = []
        for f in flags:
            tx = AccountSet(account=w.address, set_flag=f)
            p = await self.build_sign_and_track(tx, w)  # allocator hands next Sequence
            pendings.append(p)
            await self.submit_pending(p)

        for p in [p0, *pendings]:
            _ = await self.wait_for_validation(p.tx_hash, overall=15.0)

    async def _apply_gateway_flags(self, *, req_auth: bool, def_ripple: bool) -> dict[str, Any]:
        """Apply per-gateway account flags. One AccountSet per asf flag."""
        flags: list[AccountSetAsfFlag] = []
        if req_auth:
            flags.append(AccountSetAsfFlag.ASF_REQUIRE_AUTH)
        if def_ripple:
            flags.append(AccountSetAsfFlag.ASF_DEFAULT_RIPPLE)

        if not flags or not self.gateways:
            return {"applied": 0, "results": []}

        results: list[dict[str, Any]] = []
        for w in self.gateways:
            addr = w.classic_address
            for f in flags:
                tx = AccountSet(account=addr, set_flag=f)

                p = await self.build_sign_and_track(tx, w)

                res = await self.submit_pending(p, timeout=max(getattr(self, "rpc_timeout", 3.0), 15.0))

                er = (res or {}).get("engine_result")
                txh = (res or {}).get("tx_json", {}).get("hash") if res else None

                results.append(
                    {
                        "address": addr,
                        "flag": f.name,
                        "engine_result": er,
                        "tx_hash": txh,
                        "state": p.state.name,
                    }
                )

                if isinstance(er, str) and er != "tesSUCCESS":
                    log.error("AccountSet failed addr=%s flag=%s res=%s", addr, f.name, res)

        return {"applied": len(flags) * len(self.gateways), "results": results}

    async def wait_until_validated(
        self, tx_hash: str, *, overall: float = 15.0, per_rpc: float = 2.0
    ) -> dict[str, Any]:
        """Block until tx validated, rejected, or timeout. Returns the final Tx result dict.

        Invariants on success:
          - result["validated"] is True
          - result["ledger_index"] is an int
          - result["meta"]["TransactionResult"] is a str
        """
        try:
            async with asyncio.timeout(overall):
                while True:
                    r = await asyncio.wait_for(
                        self.client.request(Tx(transaction=tx_hash)),
                        timeout=per_rpc,
                    )
                    result: dict[str, Any] = r.result

                    if not result.get("validated"):
                        await asyncio.sleep(0.5)
                        continue

                    meta = result.get("meta")
                    if not isinstance(meta, dict) or "TransactionResult" not in meta:
                        raise RuntimeError(f"Validated response missing meta.TransactionResult for {tx_hash}")
                    ledger_index = result.get("ledger_index")
                    if not isinstance(ledger_index, int):
                        raise RuntimeError(f"Validated response missing integer ledger_index for {tx_hash}")

                    meta_result: str = meta["TransactionResult"]

                    await self.record_validated(
                        ValidationRecord(txn=tx_hash, seq=ledger_index, src=ValidationSrc.POLL),
                        meta_result=meta_result,
                    )
                    return result

        except TimeoutError:
            log.debug("Validation timeout tx=%s after %.1fs", tx_hash, overall)
            return {"validated": False, "timeout": True}

    async def submit_random_txn(self, n: int | None = None):
        txn = await generate_txn(self.ctx)
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
                log.debug(f"  {label}: Submitting batch of {len(to_submit)} txns ({submitted_count}/{total} total)")

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
                terminal = sum(1 for p in txns if p.state in TERMINAL_STATE)
                submitted_for_acc = account_idx[acc]
                account_inflight[acc] = submitted_for_acc - terminal

            all_terminal = all(p.state in TERMINAL_STATE for p in all_pending)
            if all_terminal:
                break

        return result_counts

    async def _establish_trust_lines(self) -> None:
        """Create TrustSet transactions from all users to all configured currencies.

        Submits in batches respecting the 10 txn per-account queue limit.
        """
        if not self.users or not self._currencies:
            log.debug("No users or currencies to establish trust lines")
            return

        trust_limit = str(self.config["transactions"]["trustset"]["limit"])
        total_trustsets = len(self.users) * len(self._currencies)

        log.debug(
            f"Establishing trust lines: {len(self.users)} users × {len(self._currencies)} currencies = {total_trustsets} TrustSets"
        )

        all_pending = []
        for user in self.users:
            for currency in self._currencies:
                trust_tx = TrustSet(
                    account=user.address,
                    limit_amount=IssuedCurrencyAmount(
                        currency=currency.currency,
                        issuer=currency.issuer,
                        value=trust_limit,
                    ),
                )
                pending = await self.build_sign_and_track(trust_tx, user)
                all_pending.append(pending)

        log.debug(f"  Built {len(all_pending)} TrustSets")

        result_counts = await self._submit_batched_by_account(all_pending, "TrustSet")
        log.debug(f"  Submission results: {dict(result_counts)}")

        validated_count = sum(1 for p in all_pending if p.state == C.TxState.VALIDATED)
        current = await self._current_ledger_index()
        log.debug(f"TrustSet complete in {current}: {validated_count}/{total_trustsets} validated")

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
                if p.state not in TERMINAL_STATE:
                    try:
                        await self.check_finality(p)
                    except Exception as e:
                        log.debug(f"check_finality failed for {p.tx_hash}: {e}")

            counts: dict[str, int] = {}
            for p in pending_list:
                state = p.state.name
                counts[state] = counts.get(state, 0) + 1

            if all(p.state in TERMINAL_STATE for p in pending_list):
                validated = counts.get("VALIDATED", 0)
                total = len(pending_list)
                log.debug(f"{label} complete: {validated}/{total} validated, {counts}")
                return counts

            await asyncio.sleep(0.5)

        counts = {}
        for p in pending_list:
            state = p.state.name
            counts[state] = counts.get(state, 0) + 1
        log.debug(f"{label} timeout after {timeout}s: {counts}")
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

        log.debug(f"{label}: {total} txns across {len(by_account)} accounts (max {max_batch_size}/batch)")

        start = time.time()
        while time.time() - start < timeout:
            total_inflight = sum(
                1 for p in pending_list if p.tx_hash in submitted_hashes and p.state not in TERMINAL_STATE
            )
            global_slots = max_batch_size - total_inflight

            if global_slots <= 0:
                await asyncio.sleep(0.5)
                for p in pending_list:
                    if p.tx_hash in submitted_hashes and p.state not in TERMINAL_STATE:
                        try:
                            await self.check_finality(p)
                        except Exception:
                            pass
                continue

            newly_submitted = []
            for acc, txns in by_account.items():
                if len(newly_submitted) >= global_slots:
                    break  # Hit global limit

                inflight = sum(1 for p in txns if p.tx_hash in submitted_hashes and p.state not in TERMINAL_STATE)
                available_slots = min(max_per_account - inflight, global_slots - len(newly_submitted))

                for p in txns:
                    if available_slots <= 0:
                        break
                    if p.tx_hash not in submitted_hashes:
                        newly_submitted.append(p)
                        submitted_hashes.add(p.tx_hash)
                        available_slots -= 1

            if newly_submitted:
                log.debug(f"  {label}: Submitting {len(newly_submitted)} txns ({len(submitted_hashes)}/{total} total)")
                for p in newly_submitted:
                    try:
                        await self.submit_pending(p)
                    except Exception as e:
                        log.error(f"Submit failed for {p.tx_hash}: {e}")

            for p in pending_list:
                if p.tx_hash in submitted_hashes and p.state not in TERMINAL_STATE:
                    try:
                        await self.check_finality(p)
                    except Exception:
                        pass

            counts: dict[str, int] = {}
            for p in pending_list:
                state = p.state.name
                counts[state] = counts.get(state, 0) + 1

            if all(p.state in TERMINAL_STATE for p in pending_list):
                validated = counts.get("VALIDATED", 0)
                log.debug(f"{label} complete: {validated}/{total} validated, {counts}")
                return counts

            await asyncio.sleep(0.5)

        counts = {}
        for p in pending_list:
            state = p.state.name
            counts[state] = counts.get(state, 0) + 1
        log.debug(f"{label} timeout after {timeout}s: {counts}")
        return counts

    async def _distribute_initial_tokens(self) -> None:
        """Gateways send initial token balances to all users.

        Submits in batches respecting the 10 txn per-account queue limit.
        """
        if not self.users or not self._currencies:
            log.debug("No users or currencies to distribute tokens")
            return

        initial_amount = str(self.config.get("currencies", {}).get("token_distribution", 1_000_000))

        all_pending = []
        for currency in self._currencies:
            issuer_wallet = self.wallets.get(currency.issuer)
            if not issuer_wallet:
                log.error(f"Cannot find wallet for gateway {currency.issuer}")
                continue

            for user in self.users:
                payment_tx = Payment(
                    account=currency.issuer,
                    destination=user.address,
                    amount=IssuedCurrencyAmount(
                        currency=currency.currency,
                        issuer=currency.issuer,
                        value=initial_amount,
                    ),
                )
                pending = await self.build_sign_and_track(payment_tx, issuer_wallet)
                all_pending.append(pending)

        total_payments = len(all_pending)
        log.debug(f"Distributing tokens: {total_payments} payments")

        result_counts = await self._submit_batched_by_account(all_pending, "TokenDist")
        log.debug(f"  Submission results: {dict(result_counts)}")

        validated_count = sum(1 for p in all_pending if p.state == C.TxState.VALIDATED)
        current = await self._current_ledger_index()
        log.debug(f"Token distribution complete at {current}: {validated_count}/{total_payments} validated")

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

        log.debug(f"{label}: {total} transactions (aggressive submission, tracking per-account pending)")

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
                    if p.state not in TERMINAL_STATE:
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

            log.debug(
                f"  {label}: Submitted {len(batch_pending)} txns ({submitted_count}/{total}, {len(remaining)} waiting, max_batch={max_batch})"
            )

            if to_submit:  # Only wait if there's more to submit
                current_ledger = await self._current_ledger_index()
                for _ in range(20):  # Safety limit
                    new_ledger = await self._current_ledger_index()
                    if new_ledger > current_ledger:
                        break
                    await asyncio.sleep(0.5)

        log.debug(f"{label}: All submitted, waiting for finality...")
        for _ in range(200):  # Max iterations as safety
            non_terminal = [p for p in all_pending if p.state not in TERMINAL_STATE]
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
        log.debug(f"{label} complete: {validated}/{total} validated, {counts}")
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
        log.debug(f"Phase 1: Funding {gateway_count} gateways")

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
            log.debug(f"Phase 2: Setting gateway flags (require_auth={req_auth}, default_ripple={def_ripple})")

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
            log.debug("Phase 2: Skipping gateway flags (none configured)")
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
        log.debug(f"Phase 3: Funding {user_count} users in batches of {batch_size}")

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

            log.debug(f"  Phase 3: Submitted batch {batch_start // batch_size + 1} ({len(batch_pending)} users)")

            for _ in range(60):
                for w, p in batch_pending:
                    if p.state not in TERMINAL_STATE:
                        try:
                            await self.check_finality(p)
                        except Exception:
                            pass
                if all(p.state in TERMINAL_STATE for _, p in batch_pending):
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
        log.debug(f"Phase 3 complete: {validated_users}/{user_count} users funded")

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
        log.debug(
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
            log.debug(f"Phase 4: Only {phase4_validated}/{phase4_total} TrustSets validated - some tokens may fail")

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
        log.debug(f"Phase 5: Seeding {seed_count} users with tokens ({total_payments} payments)")
        log.debug(f"  Formula: max(sqrt({num_users}), {num_gateways}*2) = {seed_count} seeds")
        log.debug(f"  Amount per seed: {seed_amount} ({users_per_seed} users/seed × {base_amount} × 2)")
        log.debug(f"  Remaining {num_users - seed_count} users receive tokens via fan-out during continuous workload")

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

        log.debug(f"Phase 5: Waiting for all {phase5_total} txns to reach terminal state...")
        start_ledger = await self._current_ledger_index()

        max_wait_ledgers = 20  # Safety: 20 ledgers (~70s at 3.5s/ledger)
        last_log_ledger = start_ledger

        while True:
            current_ledger = await self._current_ledger_index()

            terminal_count = sum(1 for p in phase5_pending if p.state in TERMINAL_STATE)
            pending_count = phase5_total - terminal_count

            if current_ledger >= last_log_ledger + 5:
                log.debug(
                    f"  Phase 5: {terminal_count}/{phase5_total} resolved, {pending_count} pending @ ledger {current_ledger}"
                )
                last_log_ledger = current_ledger

            if terminal_count == phase5_total:
                log.debug(f"Phase 5: All {phase5_total} txns resolved @ ledger {current_ledger}")
                break

            if current_ledger >= start_ledger + max_wait_ledgers:
                log.debug(f"Phase 5: Timeout after {max_wait_ledgers} ledgers - {pending_count} still pending")
                break

            await asyncio.sleep(1)

        phase5_counts: dict[str, int] = {}
        for p in phase5_pending:
            phase5_counts[p.state.name] = phase5_counts.get(p.state.name, 0) + 1
        phase5_validated = phase5_counts.get("VALIDATED", 0)

        log.debug(f"Phase 5: Final results: {phase5_validated}/{phase5_total} validated, {phase5_counts}")

        for user_addr, currency_code, amount, issuer in balance_updates:
            self._set_balance(user_addr, currency_code, amount, issuer)

        if phase5_validated < phase5_total:
            log.debug(f"Phase 5: Only {phase5_validated}/{phase5_total} token distributions validated")

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
        log.debug(f"Phase 6: Fan-out distribution to {len(unfunded_users)} remaining users")

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
        log.debug(
            f"  {len(unfunded_users)} users × {len(self._currencies)} currencies = {total_fanout} fan-out payments"
        )

        phase6_validated, phase6_total, phase6_pending = await self._init_batch(fanout_txn_pairs, "Phase 6: Fan-out")

        log.debug(f"Phase 6: Waiting for all {phase6_total} txns to reach terminal state...")
        start_ledger = await self._current_ledger_index()

        max_wait_ledgers = 30  # Safety: 30 ledgers (~105s) - more txns than Phase 5
        last_log_ledger = start_ledger

        while True:
            current_ledger = await self._current_ledger_index()

            terminal_count = sum(1 for p in phase6_pending if p.state in TERMINAL_STATE)
            pending_count = phase6_total - terminal_count

            if current_ledger >= last_log_ledger + 5:
                log.debug(
                    f"  Phase 6: {terminal_count}/{phase6_total} resolved, {pending_count} pending @ ledger {current_ledger}"
                )
                last_log_ledger = current_ledger

            if terminal_count == phase6_total:
                log.debug(f"Phase 6: All {phase6_total} txns resolved @ ledger {current_ledger}")
                break

            if current_ledger >= start_ledger + max_wait_ledgers:
                log.debug(f"Phase 6: Timeout after {max_wait_ledgers} ledgers - {pending_count} still pending")
                break

            await asyncio.sleep(1)

        phase6_counts: dict[str, int] = {}
        for p in phase6_pending:
            phase6_counts[p.state.name] = phase6_counts.get(p.state.name, 0) + 1
        phase6_validated = phase6_counts.get("VALIDATED", 0)

        log.debug(f"Phase 6: Final results: {phase6_validated}/{phase6_total} validated, {phase6_counts}")

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
        log.debug(
            f"Token distribution complete: {funded_users}/{len(self.users)} users, {total_received}/{total_expected} currency pairs"
        )

        self.save_currencies_to_store()

        init_end_time = time.time()
        init_end_ledger = await self._current_ledger_index()
        total_time = round(init_end_time - init_start_time, 2)
        total_ledgers = init_end_ledger - init_start_ledger
        total_validated = sum(p["validated"] for p in phase_stats)
        total_txns = sum(p["total"] for p in phase_stats)

        log.debug("=" * 60)
        log.debug("INITIALIZATION SUMMARY")
        log.debug("=" * 60)
        for p in phase_stats:
            log.debug(
                f"  {p['name']}: {p['validated']}/{p['total']} validated, {p['time_sec']}s, {p['ledgers']} ledgers"
            )
        log.debug("-" * 60)
        log.debug(f"  TOTAL: {total_validated}/{total_txns} validated, {total_time}s, {total_ledgers} ledgers")
        log.debug(f"  Final state: {len(self.gateways)} gateways, {len(self.users)} users at ledger {init_end_ledger}")
        log.debug("=" * 60)

        return {"gateways": out_gw, "users": out_us}

    async def _post(self, url: str, payload: dict):
        async with httpx.AsyncClient() as client:
            try:
                resp = await client.post(url, json=payload)
                response = resp.json()
            except Exception as e:
                pass
        try:
            response = resp.json()
        except:
            pass
        finally:
            return response

    async def validator_state(self, n: int):
        import subprocess

        val = f"val{n}"
        cmd = ["docker", "inspect", "-f", "'{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "]
        cmd.append(val)
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        val_ip = result.stdout.strip().replace("'", "")
        rpc_port = 5005
        return f"http://{val_ip}:{rpc_port}"

    def snapshot_pending(self, *, open_only: bool = True) -> list[dict]:
        OPEN_STATES = {C.TxState.CREATED, C.TxState.SUBMITTED, C.TxState.RETRYABLE, C.TxState.FAILED_NET}  # TODO: Move
        out = []
        for txh, p in self.pending.items():
            if open_only and p.state not in OPEN_STATES:
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
                    "validated_ledger": p.validated_ledger,
                    "meta_txn_result": p.meta_txn_result,
                }
            )
        return out

    def snapshot_finalized(self) -> list[dict]:
        return [r for r in self.snapshot_pending(open_only=False) if r["state"] in {s.name for s in TERMINAL_STATE}]

    def snapshot_failed(self) -> list[dict[str, Any]]:
        failed_states = {"REJECTED", "EXPIRED", "FAILED_NET"}
        return [r for r in self.snapshot_pending(open_only=False) if r["state"] in failed_states]

    def snapshot_stats(self) -> dict[str, Any]:
        by_state: dict[str, int] = {}
        for p in self.pending.values():
            state = p.state.name
            by_state[state] = by_state.get(state, 0) + 1

        return {
            "total_tracked": len(self.pending),
            "by_state": by_state,
            "gateways": len(self.gateways),
            "users": len(self.users),
        }

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


PER_TX_TIMEOUT = 3


async def periodic_finality_check(w: Workload, stop: asyncio.Event, interval: int = 5):
    while not stop.is_set():
        try:
            for p in w.find_by_state(C.TxState.SUBMITTED):
                try:
                    await w.check_finality(p)
                except Exception:
                    log.exception("[finality] check failed for %s", getattr(p, "tx_hash", p))
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("[finality] outer loop error; continuing")
            await asyncio.sleep(0.5)

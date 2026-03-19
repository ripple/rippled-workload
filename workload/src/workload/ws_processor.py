"""
WebSocket event processor that consumes events from the WS listener queue
and updates transaction states in the Workload.

This is the bridge between the passive WS listener and the active Workload state machine.
"""

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from workload.workload_core import Workload

from workload.validation import ValidationRecord, ValidationSrc

from workload.assertions import always, sometimes


log = logging.getLogger("workload.ws_processor")

# Counters for WS event matching — quantify how many validations we catch vs miss
_ws_counters: dict[str, int] = {
    "validated_matched": 0,
    "validated_unmatched": 0,
    "proposed_matched": 0,
    "proposed_unmatched": 0,
}


def get_ws_counters() -> dict[str, int]:
    return dict(_ws_counters)


async def process_ws_events(
    workload: "Workload",
    event_queue: asyncio.Queue,
    stop: asyncio.Event,
) -> None:
    """
    Consume events from WebSocket listener and update workload state.

    This task runs for the lifetime of the application, processing:
    - Transaction validations from the stream (SEQUENTIAL - parallel caused blocking)
    - Proposed transaction feedback (accounts_proposed subscription)
    - Ledger close notifications (with txn_count and fee data from WS)
    - Immediate submission responses (if/when we switch to WS submission)

    Parameters
    ----------
    workload:
        The Workload instance to update
    event_queue:
        Queue receiving events from ws_listener
    stop:
        Event to signal graceful shutdown
    """
    log.info("WS event processor starting")
    processed_count = 0

    try:
        while not stop.is_set():
            try:
                event = await asyncio.wait_for(event_queue.get(), timeout=1.0)
                event_type, data = event

                if event_type == "tx_validated":
                    await _handle_tx_validated(workload, data)
                    processed_count += 1

                elif event_type == "tx_proposed":
                    await _handle_tx_proposed(workload, data)

                elif event_type == "ledger_closed":
                    await _handle_ledger_closed(workload, data)

                elif event_type == "server_status":
                    await _handle_server_status(workload, data)

                elif event_type == "tx_response":
                    await _handle_tx_response(workload, data)

                elif event_type == "raw":
                    pass

                if processed_count > 0 and processed_count % 100 == 0:
                    log.debug(
                        "WS processor: %d validations processed (matched=%d unmatched=%d)",
                        processed_count,
                        _ws_counters["validated_matched"],
                        _ws_counters["validated_unmatched"],
                    )

            except asyncio.TimeoutError:
                continue
            except Exception as e:
                log.error("Error processing WS event: %s", e, exc_info=True)

    except asyncio.CancelledError:
        log.info("WS event processor cancelled")
        raise
    finally:
        log.info(
            "WS event processor stopped (processed %d events, counters=%s)",
            processed_count,
            _ws_counters,
        )


async def _handle_tx_validated(workload: "Workload", msg: dict) -> None:
    """
    Handle a validated transaction from the WebSocket stream.

    We subscribe to specific accounts (our wallets), so we should only receive
    transactions affecting our accounts. However, we still need to check if
    it's in pending since we receive notifications for:
    - Transactions we submitted (in pending)
    - Transactions TO our accounts from external sources (not in pending)

    Message structure:
    {
        "type": "transaction",
        "validated": true,
        "transaction": {
            "hash": "ABC123...",
            "Account": "rXXX...",
            ...
        },
        "meta": {
            "TransactionResult": "tesSUCCESS",
            ...
        },
        "ledger_index": 12345
    }
    """
    # Hash fallback: try top-level "hash" first (newer rippled), then nested "transaction.hash"
    tx_hash = msg.get("hash") or msg.get("transaction", {}).get("hash")

    if not tx_hash:
        log.debug("WS validation missing tx hash, ignoring")
        return

    pending = workload.pending.get(tx_hash)
    if not pending:
        _ws_counters["validated_unmatched"] += 1
        log.debug("WS validation for non-pending tx: %s", tx_hash)
        return

    _ws_counters["validated_matched"] += 1

    ledger_index = msg.get("ledger_index")
    meta = msg.get("meta", {})
    meta_result = meta.get("TransactionResult")

    if not ledger_index:
        log.warning("WS validation missing ledger_index for tx %s", tx_hash)
        return

    log.debug(
        "WS validation: tx=%s ledger=%s result=%s account=%s",
        tx_hash,
        ledger_index,
        meta_result,
        pending.account,
    )

    validation_record = ValidationRecord(
        txn=tx_hash,
        seq=ledger_index,
        src=ValidationSrc.WS,
    )

    try:
        await workload.record_validated(validation_record, meta_result=meta_result)
    except Exception as e:
        log.error("Failed to record WS validation for %s: %s", tx_hash, e, exc_info=True)


async def _handle_ledger_closed(workload: "Workload", msg: dict) -> None:
    """Handle a ledger close notification.

    Uses WS-provided txn_count and fee_base directly — no RPC needed.
    """
    ledger_index = msg.get("ledger_index")

    # Wake the consumer — this is the tick, no RPC needed
    if ledger_index:
        workload.notify_ledger_closed(ledger_index)
    ledger_hash = msg.get("ledger_hash")

    # txn_count comes directly from the ledgerClosed WS message — zero RPC cost
    txn_count = msg.get("txn_count")
    if txn_count is not None:
        workload.last_closed_ledger_txn_count = int(txn_count)

    # Update cached fee from WS (fee_base is in the ledgerClosed message)
    fee_base = msg.get("fee_base")
    if fee_base is not None:
        workload._cached_fee = int(fee_base)

    # Update reserve info from WS
    reserve_base = msg.get("reserve_base")
    reserve_inc = msg.get("reserve_inc")
    if reserve_base is not None:
        workload._ws_reserve_base = int(reserve_base)
    if reserve_inc is not None:
        workload._ws_reserve_inc = int(reserve_inc)

    log.debug("Ledger %s closed (hash: %s, txns: %s, fee_base: %s)", ledger_index, ledger_hash, txn_count, fee_base)

    if txn_count is None:
        # Fallback to RPC if WS doesn't provide txn_count (shouldn't happen)
        try:
            txn_count = await workload.fetch_ledger_tx_count(ledger_index)
        except Exception as e:
            log.error("Error fetching ledger %s txn count: %s", ledger_index, e)
            return
        if txn_count is None:
            return

    try:
        sometimes(
            txn_count > 0,
            "ledger_contains_transactions",
            {"ledger_index": ledger_index, "txn_count": txn_count, "ledger_hash": ledger_hash},
        )

        if workload.workload_started:
            always(
                txn_count > 0,
                "active_workload_produces_transactions",
                {"ledger_index": ledger_index, "txn_count": txn_count},
            )

    except Exception as e:
        log.error("Error in ledger %s assertions: %s", ledger_index, e)


async def _handle_tx_proposed(workload: "Workload", msg: dict) -> None:
    """Handle a proposed (unvalidated) transaction from accounts_proposed subscription.

    This gives us early feedback when rippled processes our txn — we see engine_result
    (e.g. tesSUCCESS, tefPAST_SEQ) immediately via WS, before waiting for validation.
    """
    tx_hash = msg.get("hash") or msg.get("transaction", {}).get("hash")
    engine_result = msg.get("engine_result")

    if not tx_hash:
        return

    pending = workload.pending.get(tx_hash)
    if not pending:
        _ws_counters["proposed_unmatched"] += 1
        return

    _ws_counters["proposed_matched"] += 1

    # Log early feedback — especially useful for tefPAST_SEQ detection
    if engine_result and engine_result != "tesSUCCESS":
        log.info(
            "WS proposed: tx=%s result=%s type=%s account=%s seq=%s",
            tx_hash,
            engine_result,
            pending.transaction_type,
            pending.account,
            pending.sequence,
        )
    else:
        log.debug("WS proposed: tx=%s result=%s", tx_hash, engine_result)


async def _handle_tx_response(workload: "Workload", msg: dict) -> None:
    """
    Handle immediate submission response from WebSocket.

    This is for FUTURE USE when we switch to WebSocket-based submission.
    Currently submissions go via RPC, so this won't fire.

    Message structure (after WS submit):
    {
        "engine_result": "tesSUCCESS",
        "engine_result_code": 0,
        "engine_result_message": "The transaction was applied...",
        "tx_json": {
            "hash": "ABC123...",
            ...
        },
        ...
    }
    """
    engine_result = msg.get("engine_result")
    tx_json = msg.get("tx_json", {})
    tx_hash = tx_json.get("hash")

    if not tx_hash:
        log.debug("WS response missing tx hash")
        return

    log.info("WS submission response: tx=%s result=%s", tx_hash, engine_result)


async def _handle_server_status(workload: "Workload", msg: dict) -> None:
    """Handle server status updates — delegates state update to Workload."""
    workload.update_server_status(msg)
    computed = workload.latest_server_status_computed
    log.debug(
        "Server status: %s | Load: %.1fx | Queue: %.1fx | Open ledger: %.1fx",
        computed.get("server_status", "unknown"),
        computed.get("general_load_multiplier", 1.0),
        computed.get("queue_fee_multiplier", 1.0),
        computed.get("open_ledger_fee_multiplier", 1.0),
    )

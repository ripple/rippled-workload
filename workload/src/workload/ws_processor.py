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

import workload.constants as C
from workload.workload_core import ValidationRecord, ValidationSrc

try:
    from antithesis.assertions import always, sometimes

    ANTITHESIS_AVAILABLE = True
except ImportError:
    ANTITHESIS_AVAILABLE = False

    def sometimes(condition, message, details=None):
        pass

    def always(condition, message, details=None):
        pass


log = logging.getLogger("workload.ws_processor")


async def process_ws_events(
    workload: "Workload",
    event_queue: asyncio.Queue,
    stop: asyncio.Event,
) -> None:
    """
    Consume events from WebSocket listener and update workload state.

    This task runs for the lifetime of the application, processing:
    - Transaction validations from the stream (SEQUENTIAL - parallel caused blocking)
    - Ledger close notifications
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
    log.debug("WS event processor starting")
    processed_count = 0

    try:
        while not stop.is_set():
            try:
                event = await asyncio.wait_for(event_queue.get(), timeout=1.0)
                event_type, data = event

                if event_type == "tx_validated":
                    await _handle_tx_validated(workload, data)
                    processed_count += 1

                elif event_type == "ledger_closed":
                    await _handle_ledger_closed(workload, data)

                elif event_type == "server_status":
                    await _handle_server_status(workload, data)

                elif event_type == "tx_response":
                    await _handle_tx_response(workload, data)

                elif event_type == "raw":
                    pass

                if processed_count > 0 and processed_count % 100 == 0:
                    log.debug("WS processor: %d validations processed", processed_count)

            except asyncio.TimeoutError:
                continue
            except Exception as e:
                log.error("Error processing WS event: %s", e, exc_info=True)

    except asyncio.CancelledError:
        log.debug("WS event processor cancelled")
        raise
    finally:
        log.debug("WS event processor stopped (processed %d events)", processed_count)


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
    tx_data = msg.get("transaction", {})
    tx_hash = tx_data.get("hash")

    if not tx_hash:
        log.debug("WS validation missing tx hash, ignoring")
        return

    pending = workload.pending.get(tx_hash)
    if not pending:
        log.debug("WS validation for non-pending tx affecting our accounts: %s", tx_hash[:8])
        return

    ledger_index = msg.get("ledger_index")
    meta = msg.get("meta", {})
    meta_result = meta.get("TransactionResult")

    if not ledger_index:
        log.debug("WS validation missing ledger_index for tx %s", tx_hash)
        return

    log.debug(
        "WS validation: tx=%s ledger=%s result=%s account=%s",
        tx_hash[:8],
        ledger_index,
        meta_result,
        pending.account[:8],
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
    """
    Handle a ledger close notification.

    Fetches ledger transaction count and runs Antithesis assertions to validate:
    - Ledgers contain transactions at least sometimes (workload is functioning)
    - Network is processing submitted transactions

    Also submits heartbeat transaction for this ledger.

    Message structure:
    {
        "type": "ledgerClosed",
        "ledger_index": 12345,
        "ledger_hash": "ABC123...",
        "ledger_time": 742623456,
        ...
    }
    """
    ledger_index = msg.get("ledger_index")
    ledger_hash = msg.get("ledger_hash")

    log.debug("Ledger %s closed (hash: %s)", ledger_index, ledger_hash)

    try:
        from xrpl.models.requests import Ledger

        ledger_req = Ledger(
            ledger_index=ledger_index,
            transactions=True,  # Include tx hashes (not full txns)
            expand=False,  # Don't expand to full transaction objects
        )

        ledger_resp = await workload._rpc(ledger_req)

        if ledger_resp.is_successful():
            ledger_data = ledger_resp.result.get("ledger", {})
            transactions = ledger_data.get("transactions", [])
            txn_count = len(transactions)

            log.debug("Ledger %s closed with %d transactions", ledger_index, txn_count)

            sometimes(
                txn_count > 0,
                "ledger_contains_transactions",
                {
                    "ledger_index": ledger_index,
                    "txn_count": txn_count,
                    "ledger_hash": ledger_hash,
                },
            )

            if hasattr(workload, "_workload_started") and workload._workload_started:
                always(
                    txn_count > 0,
                    "active_workload_produces_transactions",
                    {
                        "ledger_index": ledger_index,
                        "txn_count": txn_count,
                    },
                )
        else:
            log.debug("Failed to fetch ledger %s: %s", ledger_index, ledger_resp.result)

    except Exception as e:
        log.error("Error fetching ledger %s for assertions: %s", ledger_index, e)


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

    log.debug("WS submission response: tx=%s result=%s", tx_hash[:8], engine_result)


async def _handle_server_status(workload: "Workload", msg: dict) -> None:
    """Handle server status updates from the server stream.

    Stores the raw message and computes human-readable fee multipliers.

    Message structure:
    {
        "type": "serverStatus",
        "server_status": "normal|full|busy|...",
        "load_base": 256,
        "load_factor": 256,
        "load_factor_server": 256,
        "load_factor_fee_escalation": 256,
        "load_factor_fee_queue": 256,
        "load_factor_fee_reference": 256,
        ...
    }
    """
    import time

    workload.latest_server_status = msg
    workload.latest_server_status_time = time.time()

    load_factor = msg.get("load_factor", 256)
    load_factor_fee_escalation = msg.get("load_factor_fee_escalation")
    load_factor_fee_queue = msg.get("load_factor_fee_queue")
    load_factor_fee_reference = msg.get("load_factor_fee_reference", 256)
    server_status = msg.get("server_status", "unknown")

    queue_multiplier = load_factor_fee_queue / load_factor_fee_reference if load_factor_fee_queue else 1.0
    escalation_multiplier = (
        load_factor_fee_escalation / load_factor_fee_reference if load_factor_fee_escalation else 1.0
    )
    general_load_multiplier = load_factor / 256.0

    workload.latest_server_status_computed = {
        "server_status": server_status,
        "queue_fee_multiplier": queue_multiplier,
        "open_ledger_fee_multiplier": escalation_multiplier,
        "general_load_multiplier": general_load_multiplier,
    }

    log.debug(
        "Server status: %s | Load: %.1fx | Queue: %.1fx | Open ledger: %.1fx",
        server_status,
        general_load_multiplier,
        queue_multiplier,
        escalation_multiplier,
    )

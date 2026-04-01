"""WebSocket transaction observer.

Subscribes to the xrpld transactions stream and processes all validated
transaction results. Fires Antithesis assertions and updates workload
state (vaults, NFTs, etc.) from the validated metadata.

State updaters are defined in transactions/__init__.py (the registry).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from workload.app import Workload

from xrpl.asyncio.clients import AsyncWebsocketClient
from xrpl.models import StreamParameter, Subscribe

from workload import logging
from workload.assertions import tx_result
from workload.transactions import STATE_UPDATERS

log = logging.getLogger(__name__)


def _handle_validated_tx(workload: Workload, msg: dict) -> None:
    """Process a single validated transaction from the WS stream."""
    meta = msg.get("meta", {})
    tx = msg.get("tx_json", {})
    tx_type = tx.get("TransactionType", "")
    engine_result = msg.get("engine_result") or meta.get("TransactionResult", "")
    tx_hash = msg.get("hash", "")
    account = tx.get("Account", "")

    # Only process transactions from our accounts
    if account not in workload.accounts:
        return

    # Build a normalized result dict for tx_result()
    result = {
        "engine_result": engine_result,
        "engine_result_message": "",
        "tx_json": tx,
        "hash": tx_hash,
        "meta": meta,
    }
    tx_result(tx_type, result)

    # Update state on success
    if engine_result == "tesSUCCESS":
        updater = STATE_UPDATERS.get(tx_type)
        if updater:
            try:
                updater(workload, tx, meta)
            except Exception as e:
                log.error("WS: state update failed for %s: %s", tx_type, e)


async def start_ws_listener(workload: Workload, ws_url: str) -> None:
    """Subscribe to validated transactions and process results.

    Runs forever with automatic reconnection. Start as a background task.
    """
    log.info("WS listener connecting to %s", ws_url)
    while True:
        try:
            async with AsyncWebsocketClient(ws_url) as ws:
                await ws.send(Subscribe(streams=[StreamParameter.TRANSACTIONS]))
                log.info("WS listener subscribed to transactions stream")
                async for msg in ws:
                    if msg.get("type") == "transaction" and msg.get("validated"):
                        _handle_validated_tx(workload, msg)
        except Exception as e:
            log.warning("WS listener disconnected: %s, reconnecting in 2s...", e)
            await asyncio.sleep(2)

"""
WebSocket listener that:
1. Maintains persistent connection to rippled WS endpoint
2. Subscribes to transaction and ledger streams
3. Publishes events to a queue for workload processing
4. Handles reconnection with exponential backoff
"""

import asyncio
import json
import logging
from typing import Literal

import websockets

log = logging.getLogger("workload.ws")

RECV_TIMEOUT = 90.0
RECONNECT_BASE = 1.0
RECONNECT_MAX = 10.0

EventType = Literal["tx_validated", "tx_response", "ledger_closed", "server_status", "raw"]


async def ws_listener(
    stop: asyncio.Event,
    ws_url: str,
    event_queue: asyncio.Queue,
    accounts_provider: callable = None,
) -> None:
    """
    Connect to rippled WebSocket, subscribe to streams, and publish events to queue.

    Parameters
    ----------
    stop:
        Event to signal graceful shutdown
    ws_url:
        WebSocket URL (e.g., "ws://rippled:6006")
    event_queue:
        Queue to publish parsed events for workload consumption
    accounts_provider:
        Optional callable that returns list of account addresses to subscribe to.
        If None, subscribes to all transactions (inefficient but simple).
    """
    backoff = RECONNECT_BASE

    while not stop.is_set():
        try:
            async with websockets.connect(ws_url, ping_interval=20, ping_timeout=20, close_timeout=1) as ws:
                log.debug("WS connected: %s", ws_url)

                accounts = None
                if accounts_provider:
                    try:
                        accounts = accounts_provider()
                        if accounts:
                            log.debug(f"Got {len(accounts)} accounts from provider")
                        else:
                            log.debug("No accounts available yet (likely during startup)")
                    except Exception as e:
                        log.debug(f"Failed to get accounts from provider: {e}, falling back to transaction stream")

                if accounts:
                    streams = ["ledger", "server"]  # TODO: Abstract out subscriptions
                    subscribe_msg = {"id": 1, "command": "subscribe", "streams": streams, "accounts": accounts}
                    if len(streams) > 2:
                        steams_string = ", ".join(streams[:-1]) + f"and {streams[-1]}"
                    elif len(streams) == 2:
                        steams_string = " and ".join(streams)
                    else:
                        steams_string, _ = streams
                    log.debug("Subscribing to %s + %s specific accounts", steams_string, len(accounts))
                else:
                    subscribe_msg = {"id": 1, "command": "subscribe", "streams": ["transactions", "ledger", "server"]}
                    log.debug(
                        "Subscribing to ALL transactions + ledger + server (fallback - will switch to accounts after init/reconnect)"
                    )

                await ws.send(json.dumps(subscribe_msg))

                try:
                    ack = await asyncio.wait_for(ws.recv(), timeout=10)
                    ack_obj = json.loads(ack)
                    if ack_obj.get("status") != "success":
                        raise RuntimeError(f"subscribe failed: {ack}")
                    log.debug("WS subscription successful")
                except asyncio.TimeoutError:
                    log.debug("WS subscription ack timeout, continuing anyway")

                backoff = RECONNECT_BASE

                while not stop.is_set():
                    recv_task = asyncio.create_task(ws.recv())
                    halt_task = asyncio.create_task(stop.wait())

                    done, pending = await asyncio.wait({recv_task, halt_task}, return_when=asyncio.FIRST_COMPLETED)

                    for t in pending:
                        t.cancel()

                    if halt_task in done:
                        log.debug("WS listener received stop signal")
                        return

                    try:
                        msg = recv_task.result()
                        await _process_message(msg, event_queue)
                    except Exception as e:
                        log.error("Error processing WS message: %s", e, exc_info=True)

        except asyncio.CancelledError:
            log.debug("WS listener cancelled")
            raise
        except Exception as e:
            log.error("WS connection error: %s", e)

        if stop.is_set():
            break

        log.debug("WS reconnecting in %.1fs", backoff)
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, RECONNECT_MAX)

    log.debug("WS listener stopped")


async def _process_message(raw_msg: str, queue: asyncio.Queue) -> None:
    """
    Parse WebSocket message and publish appropriate event to queue.

    Message types we handle:
    - type="transaction" + validated=true → tx_validated event
    - type="ledgerClosed" → ledger_closed event
    - engine_result present → tx_response event (immediate submission feedback)
    """
    try:
        obj = json.loads(raw_msg)
    except json.JSONDecodeError:
        log.debug("WS raw (non-JSON): %s", raw_msg[:200])
        await queue.put(("raw", raw_msg))
        return

    msg_type = obj.get("type")

    if msg_type == "transaction" and obj.get("validated"):
        log.debug("WS tx_validated: hash=%s ledger=%s", obj.get("transaction", {}).get("hash"), obj.get("ledger_index"))
        await queue.put(("tx_validated", obj))
        return

    if msg_type == "ledgerClosed":
        ledger_idx = obj.get("ledger_index")
        log.debug("WS ledger_closed: %s", ledger_idx)
        await queue.put(("ledger_closed", obj))
        return

    if msg_type == "serverStatus":
        log.debug("WS server_status: load_factor=%s", obj.get("load_factor"))
        await queue.put(("server_status", obj))
        return

    engine_result = obj.get("engine_result")
    if engine_result:
        log.debug("WS tx_response: result=%s", engine_result)
        await queue.put(("tx_response", obj))
        return

    status = obj.get("status")
    if status:
        log.debug("WS status: %s", status)
        return

    log.debug("WS unknown message type: %s", obj.get("type") or "no_type")

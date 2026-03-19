import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
import time
from time import perf_counter

import httpx
from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from xrpl.asyncio.clients import AsyncJsonRpcClient, AsyncWebsocketClient
from xrpl.models import StreamParameter, Subscribe
from xrpl.models.transactions import Payment

from workload.ws import ws_listener
from workload.ws_processor import get_ws_counters, process_ws_events

from workload.assertions import setup_complete


import workload.constants as C
from workload.config import cfg
from workload.logging_config import setup_logging
from workload.workload_core import PendingTx, Workload, periodic_dex_metrics, periodic_finality_check

setup_logging()
log = logging.getLogger("workload.app")


def _log_task_exception(task: asyncio.Task) -> None:
    """Done callback: log unhandled exceptions from fire-and-forget tasks."""
    if not task.cancelled() and (exc := task.exception()):
        log.error("Task %r crashed: %s: %s", task.get_name(), type(exc).__name__, exc, exc_info=exc)


if Path("/.dockerenv").is_file():
    rippled = cfg["rippled"]["docker"]
else:
    rippled = cfg["rippled"]["local"]

rpc_port = cfg["rippled"]["rpc_port"]
ws_port = cfg["rippled"]["ws_port"]
rippled_ip = os.getenv("RIPPLED_IP", rippled)

RPC = os.getenv("RPC_URL", f"http://{rippled_ip}:{rpc_port}")
WS = os.getenv("WS_URL", f"ws://{rippled_ip}:{ws_port}")

to = cfg["timeout"]
TIMEOUT = 3.0
OVERALL = to["overall"]
OVERALL_STARTUP_TIMEOUT = to["startup"]
LEDGERS_TO_WAIT = to["initial_ledgers"]


async def _probe_rippled(url: str, max_retries: int = 30, retry_delay: float = 2.0) -> None:
    """Probe rippled RPC endpoint with retries until it responds.


    Args:
        url: RPC endpoint URL
        max_retries: Maximum number of retry attempts (default: 30 = 1 minute with 2s delay)
        retry_delay: Seconds to wait between retries
    """
    payload = {"method": "server_info", "params": [{}]}

    for attempt in range(1, max_retries + 1):
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as http:
                r = await http.post(url, json=payload)
                r.raise_for_status()
                log.info(f"RPC endpoint responding (attempt {attempt}/{max_retries})")
                return
        except Exception as e:
            if attempt < max_retries:
                log.info(
                    f"RPC not ready yet (attempt {attempt}/{max_retries}): {e.__class__.__name__} - retrying in {retry_delay}s..."
                )
                await asyncio.sleep(retry_delay)
            else:
                log.error(
                    "\n"
                    "========================================================\n"
                    f"  Cannot reach rippled at {url}\n"
                    f"  Failed after {max_retries} attempts ({e.__class__.__name__})\n"
                    "\n"
                    "  Is the network running? Start it with:\n"
                    "    cd /path/to/testnet && docker compose up -d\n"
                    "\n"
                    "  Or point at a different node:\n"
                    "    RPC_URL=http://<host>:5005 WS_URL=ws://<host>:6006\n"
                    "========================================================"
                )
                raise SystemExit(1)


async def wait_for_ledgers(url: str, count: int, timeout: float = 30.0) -> None:
    """
    Connects to the rippled WebSocket and waits for 'count' ledgers to close.
    """
    log.info(f"Connecting to WebSocket {url} to wait for {count} ledgers...")
    try:
        async with asyncio.timeout(timeout):
            async with AsyncWebsocketClient(url) as client:
                await client.send(Subscribe(streams=[StreamParameter.LEDGER]))
                ledger_count = 0
                async for msg in client:
                    if msg.get("type") == "ledgerClosed":
                        ledger_count += 1
                        log.debug("Ledger %s closed. (%s/%s)", msg.get("ledger_index"), ledger_count, count)
                        if ledger_count >= count:
                            log.info("Observed %s ledgers closed. Convinced network is progessing.", ledger_count)
                            break
    except Exception as e:
        log.error(
            "\n"
            "========================================================\n"
            f"  Cannot connect to rippled WebSocket at {url}\n"
            f"  {e.__class__.__name__}: {e}\n"
            "\n"
            "  The RPC endpoint responded but the WebSocket is not\n"
            "  reachable. Check that port 6006 is exposed/forwarded.\n"
            "========================================================"
        )
        raise SystemExit(1)


@asynccontextmanager
async def lifespan(app: FastAPI):
    check_interval = 2
    stop = asyncio.Event()

    log.info("=" * 60)
    log.info("XRPL Workload starting up...")
    log.info("  RPC: %s", RPC)
    log.info("  WS:  %s", WS)
    log.info("=" * 60)

    async with asyncio.timeout(OVERALL_STARTUP_TIMEOUT):
        log.info("[1/4] Probing RPC endpoint...")
        await _probe_rippled(RPC)
        log.info("[2/4] Waiting for ledger progress (%d closes)...", LEDGERS_TO_WAIT)
        await wait_for_ledgers(WS, LEDGERS_TO_WAIT)

    log.info(
        "[3/4] Network confirmed. Loading state...\n"
        "  Dashboard:  http://localhost:8000/state/dashboard\n"
        "  API docs:   http://localhost:8000/docs\n"
        "  Explorer:   https://custom.xrpl.org/localhost:6006"
    )

    from workload.sqlite_store import SQLiteStore

    client = AsyncJsonRpcClient(RPC)
    use_sqlite = os.getenv("WORKLOAD_PERSIST", "0") == "1"
    if use_sqlite:
        sqlite_store = SQLiteStore(db_path="state.db")
        log.info("SQLite persistence enabled (WORKLOAD_PERSIST=1)")
    else:
        sqlite_store = None
        log.info("SQLite persistence disabled (set WORKLOAD_PERSIST=1 to enable)")
    app.state.workload = Workload(cfg, client, store=sqlite_store)
    app.state.stop = stop

    app.state.ws_queue = asyncio.Queue(maxsize=1000)  # TODO: Constant
    log.debug("Created WS event queue (maxsize=1000)")

    async with asyncio.TaskGroup() as tg:
        tg.create_task(
            ws_listener(
                app.state.stop, WS, app.state.ws_queue, accounts_provider=app.state.workload.get_all_account_addresses
            ),
            name="ws_listener",
        )

        tg.create_task(
            periodic_finality_check(app.state.workload, app.state.stop, check_interval), name="finality_checker"
        )

        tg.create_task(
            process_ws_events(app.state.workload, app.state.ws_queue, app.state.stop),
            name="ws_processor",
        )

        tg.create_task(
            periodic_dex_metrics(app.state.workload, app.state.stop),
            name="dex_metrics_poller",
        )

        log.info("Background tasks started: ws_listener, finality_checker, ws_processor, dex_metrics_poller")

        log.debug("Checking for persisted state...")
        state_loaded = app.state.workload.load_state_from_store()

        if state_loaded:
            log.info("Loaded existing state from database, skipping network provisioning")
            log.info(
                "  Wallets: %s (Gateways: %s, Users: %s)",
                len(app.state.workload.wallets),
                len(app.state.workload.gateways),
                len(app.state.workload.users),
            )
        else:
            # Try loading from pre-generated genesis accounts
            genesis_cfg = cfg.get("genesis", {})
            accounts_json = genesis_cfg.get("accounts_json", "")

            if accounts_json:
                from pathlib import Path

                # Resolve relative to CWD (typically workload/ project root)
                accounts_path = Path(accounts_json)
                if not accounts_path.is_absolute() and not accounts_path.exists():
                    # Try relative to app.py's directory as fallback
                    accounts_path = Path(__file__).parent / accounts_json
                if not accounts_path.exists():
                    # Try absolute from repo root
                    accounts_path = (
                        Path(__file__).parent.parent.parent.parent / "prepare-workload" / "testnet" / "accounts.json"
                    )
                log.info("Genesis accounts path: %s (exists=%s)", accounts_path, accounts_path.exists())

                genesis_loaded = await app.state.workload.load_from_genesis(str(accounts_path))
            else:
                genesis_loaded = False

            if genesis_loaded:
                log.info(
                    "Loaded from genesis: %s gateways, %s users, %s AMM pools",
                    len(app.state.workload.gateways),
                    len(app.state.workload.users),
                    len(app.state.workload.amm.pools),
                )
            else:
                gw, u = cfg["gateways"], cfg["users"]
                log.info("No persisted/genesis state. Initializing participants (gateways=%s, users=%s)...", gw, u)
                init_result = await app.state.workload.init_participants(gateway_cfg=gw, user_cfg=u)
                app.state.workload.update_txn_context()
                log.info(
                    "Accounts initialized: %s gateways, %s users.",
                    len(init_result["gateways"]),
                    len(init_result["users"]),
                )

        init_ledger = await app.state.workload._current_ledger_index()
        app.state.workload.first_ledger_index = init_ledger
        setup_complete(
            {
                "gateways": len(app.state.workload.gateways),
                "users": len(app.state.workload.users),
                "total_wallets": len(app.state.workload.wallets),
                "currencies": len(app.state.workload.ctx.currencies),
                "available_txn_types": app.state.workload.ctx.config.get("transactions", {}).get("available", []),
                "state_loaded_from_db": state_loaded,
                "mptoken_ids": len(app.state.workload._mptoken_issuance_ids),
                "init_completed_ledger": init_ledger,
            }
        )
        log.info("[4/4] Initialization complete at ledger %d. Starting workload.", init_ledger)
        await asyncio.sleep(5)
        await start_workload()
        try:
            yield
        finally:
            log.info("Shutting down...")

            # Stop workload first to prevent new submissions during flush
            if workload_running and workload_stop_event:
                log.info("Stopping workload...")
                workload_stop_event.set()
                if workload_task:
                    try:
                        await asyncio.wait_for(workload_task, timeout=3.0)
                    except (TimeoutError, asyncio.CancelledError, Exception):
                        log.info("Workload task didn't stop cleanly, continuing shutdown")

            stop.set()

            # Flush in-memory state to SQLite before exit (skip if no persistent store)
            if app.state.workload.persistent_store is not None:
                log.info("Flushing state to persistent store... (Ctrl-C again to skip)")
                try:
                    await app.state.workload.flush_to_persistent_store()
                except (asyncio.CancelledError, KeyboardInterrupt):
                    log.warning("Flush interrupted, skipping")

            await asyncio.sleep(0.5)
            log.info("Exiting TaskGroup (will cancel any remaining tasks)...")

    log.info("Shutdown complete")


app = FastAPI(
    title="XRPL Workload",
    lifespan=lifespan,
    openapi_tags=[
        {"name": "Accounts", "description": "Create and query accounts"},
        {"name": "Payments", "description": "Send and track payments"},
        {"name": "Transactions", "description": "Transactions"},
        {"name": "State", "description": "Send and track general state"},
    ],
    swagger_ui_parameters={
        "tagsSorter": "alpha",  # See what "order" does...
        "operationsSorter": "alpha",  # See what "method" does...
    },
)

r_accounts = APIRouter(prefix="/accounts", tags=["Accounts"])
r_pay = APIRouter(prefix="/payment", tags=["Payments"])
r_transaction = APIRouter(tags=["Transactions"])
r_state = APIRouter(prefix="/state", tags=["State"])
r_workload = APIRouter(prefix="/workload", tags=["Workload"])
r_dex = APIRouter(prefix="/dex", tags=["DEX"])


class TxnReq(BaseModel):
    type: str


class CreateAccountReq(BaseModel):
    seed: str | None = None
    address: str | None = None
    drops: int | None = None
    algorithm: str | None = None
    wait: bool | None = False


class CreateAccountResp(BaseModel):
    address: str
    seed: str | None = None
    funded: bool
    tx_hash: str | None = None


class SendPaymentReq(BaseModel):
    source: str
    destination: str
    amount: str | dict  # XRP drops as string, or IOU as {"currency": "USD", "issuer": "r...", "value": "100"}


@app.get("/health")
def health():
    return {"status": "ok"}  # Not the most thorough of healthchecks...


@r_accounts.get("")
async def list_accounts():
    """List all accounts available to the workload.

    Returns the total count, breakdown by role (funding, gateway, user),
    and the addresses in each pool. These are the accounts used by
    generate_txn() for random transaction generation.
    """
    wl = app.state.workload
    pending_counts = wl.get_pending_txn_counts_by_account()
    available = sum(1 for addr in wl.wallets if pending_counts.get(addr, 0) < wl.max_pending_per_account)

    return {
        "total_wallets": len(wl.wallets),
        "available_for_txn": available,
        "max_pending_per_account": wl.max_pending_per_account,
        "funding": wl.funding_wallet.address,
        "gateways": {
            "count": len(wl.gateways),
            "addresses": [g.address for g in wl.gateways],
            "names": wl.gateway_names,
        },
        "users": {
            "count": len(wl.users),
            "addresses": [u.address for u in wl.users],
        },
    }


@r_accounts.get("/create")
async def api_create_account():
    return await app.state.workload.create_account()


@r_accounts.post("/create", response_model=CreateAccountResp)
async def accounts_create(req: CreateAccountReq):
    data = req.model_dump(exclude_unset=True)
    return await app.state.workload.create_account(data, wait=req.wait)


@r_accounts.get("/create/random", response_model=CreateAccountResp)
async def accounts_create_random():
    return await app.state.workload.create_account({}, wait=False)


@r_accounts.get("/{account_id}")
async def get_account_info(account_id: str):
    """Get account_info for a specific account."""
    from xrpl.models.requests import AccountInfo

    w: Workload = app.state.workload
    try:
        result = await w.client.request(AccountInfo(account=account_id, ledger_index="validated"))
        return result.result
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Account not found or error: {str(e)}")


@r_accounts.get("/{account_id}/balances")
async def get_account_balances(account_id: str):
    """Get all balances for a specific account from database."""
    from workload.sqlite_store import SQLiteStore

    w: Workload = app.state.workload
    if isinstance(w.persistent_store, SQLiteStore):
        balances = w.persistent_store.get_balances(account_id)
        return {"account": account_id, "balances": balances}
    else:
        raise HTTPException(status_code=503, detail="Balance tracking not available (no persistent store)")


@r_accounts.get("/{account_id}/lines")
async def get_account_lines(account_id: str):
    """Get trust lines for a specific account from the ledger."""
    from xrpl.models.requests import AccountLines

    w: Workload = app.state.workload
    try:
        result = await w.client.request(AccountLines(account=account_id, ledger_index="validated"))
        return result.result
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Account lines not found or error: {str(e)}")


@r_pay.post("")
async def send_payment(req: SendPaymentReq):
    """Send a payment from source to destination. Works for both XRP and issued currencies."""
    w: Workload = app.state.workload

    source_wallet = w.wallets.get(req.source)
    if not source_wallet:
        raise HTTPException(status_code=404, detail=f"Source wallet not found: {req.source}")

    payment = Payment(
        account=req.source,
        destination=req.destination,
        amount=req.amount,
    )

    pending = await w.build_sign_and_track(payment, source_wallet)
    result = await w.submit_pending(pending)

    return {
        "tx_hash": pending.tx_hash,
        "engine_result": result.get("engine_result") if result else None,
        "state": pending.state.name,
        "source": req.source,
        "destination": req.destination,
        "amount": req.amount,
    }


@r_transaction.get("/random")
async def transaction_random():
    w = app.state.workload
    res = await w.submit_random_txn()
    return res


@r_transaction.get("/create/{transaction}")
async def create(transaction: str):
    w: Workload = app.state.workload
    log.debug("Creating a %s", transaction)
    r = await w.create_transaction(transaction)
    return r


@r_transaction.post("/payment")
async def create_payment():
    """Create and submit a Payment transaction."""
    return await create("Payment")


@r_transaction.post("/trustset")
async def create_trustset():
    """Create and submit a TrustSet transaction."""
    return await create("TrustSet")


@r_transaction.post("/accountset")
async def create_accountset():
    """Create and submit an AccountSet transaction."""
    return await create("AccountSet")


@r_transaction.post("/ammcreate")
async def create_ammcreate():
    """Create and submit an AMMCreate transaction."""
    return await create("AMMCreate")


@r_transaction.post("/nftokenmint")
async def create_nftokenmint():
    """Create and submit an NFTokenMint transaction."""
    return await create("NFTokenMint")


@r_transaction.post("/mptokenissuancecreate")
async def create_mptokenissuancecreate():
    """Create and submit an MPTokenIssuanceCreate transaction."""
    return await create("MPTokenIssuanceCreate")


@r_transaction.post("/mptokenissuanceset")
async def create_mptokenissuanceset():
    """Create and submit an MPTokenIssuanceSet transaction."""
    return await create("MPTokenIssuanceSet")


@r_transaction.post("/mptokenauthorize")
async def create_mptokenauthorize():
    """Create and submit an MPTokenAuthorize transaction."""
    return await create("MPTokenAuthorize")


@r_transaction.post("/mptokenissuancedestroy")
async def create_mptokenissuancedestroy():
    """Create and submit an MPTokenIssuanceDestroy transaction."""
    return await create("MPTokenIssuanceDestroy")


@r_transaction.post("/batch")
async def create_batch():
    """Create and submit a Batch transaction."""
    return await create("Batch")


@app.post("/debug/fund")
async def debug_fund(dest: str):
    """Manually fund an address from the workload's configured `funding_account` and return the unvalidated result."""
    w: Workload = app.state.workload
    log.debug(
        "funding_wallet %s",
        w.funding_wallet.address,
    )
    fund_tx = Payment(
        account=w.funding_wallet.address,
        destination=dest,
        amount=str(1_000_000_000),
    )
    log.debug("submitting payment...")
    log.debug(json.dumps(fund_tx.to_dict(), indent=2))
    p = await w.build_sign_and_track(fund_tx, w.funding_wallet)
    log.debug("bsat: %s", p)
    res = await w.submit_pending(p)
    log.debug("response from submit_pending() %s", res)
    return res


@r_state.get("/summary")
async def state_summary():
    return app.state.workload.snapshot_stats()


@r_state.get("/dashboard", response_class=HTMLResponse)
async def state_dashboard():
    """HTML dashboard with live stats, explorer embed, and WS terminal."""
    hostname = RPC.split("//")[1].split(":")[0] if "//" in RPC else RPC.split(":")[0]

    # Build node list from compose config for the WS terminal dropdown
    nodes = []
    try:
        from gl.config import ComposeConfig

        cc = ComposeConfig()
        for i in range(cc.num_validators):
            name = f"{cc.validator_name}{i}"
            ws = cc.ws_port + i + cc.num_hubs
            nodes.append({"name": name, "ws": ws})
        for i in range(cc.num_hubs):
            name = cc.hub_name if cc.num_hubs == 1 else f"{cc.hub_name}{i}"
            ws = cc.ws_port + i
            nodes.append({"name": name, "ws": ws})
    except ImportError:
        log.debug("gl (generate_ledger) not installed, WS terminal node list will be empty")
    nodes_json = json.dumps(nodes)

    html_content = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Workload Dashboard</title>
        <style>
            * {{ box-sizing: border-box; }}
            body {{
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                background: #0d1117;
                color: #c9d1d9;
                margin: 0;
                padding: 20px;
            }}
            .container {{ max-width: 1400px; margin: 0 auto; }}
            h1 {{ color: #58a6ff; margin-bottom: 10px; }}
            h2 {{ color: #c9d1d9; margin: 0 0 12px 0; font-size: 16px; }}
            .subtitle {{ color: #8b949e; margin-bottom: 20px; }}
            .subtitle a {{ color: #58a6ff; text-decoration: none; }}
            .subtitle a:hover {{ text-decoration: underline; }}

            .stats-grid {{
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
                gap: 16px;
                margin-bottom: 20px;
            }}
            .stat-card {{
                background: #161b22; border: 1px solid #30363d;
                border-radius: 6px; padding: 16px;
            }}
            .stat-label {{
                color: #8b949e; font-size: 11px;
                text-transform: uppercase; margin-bottom: 6px;
            }}
            .stat-value {{
                font-size: 28px; font-weight: bold; margin-bottom: 2px;
            }}
            .stat-value.success {{ color: #3fb950; }}
            .stat-value.error {{ color: #f85149; }}
            .stat-value.warning {{ color: #d29922; }}
            .stat-value.info {{ color: #58a6ff; }}
            .stat-percentage {{ color: #8b949e; font-size: 13px; }}
            .progress-bar {{
                background: #21262d; border-radius: 6px;
                height: 6px; overflow: hidden; margin-top: 6px;
            }}
            .progress-fill {{ height: 100%; transition: width 0.3s ease; }}
            .progress-fill.success {{ background: #3fb950; }}
            .progress-fill.error {{ background: #f85149; }}
            .progress-fill.info {{ background: #58a6ff; }}

            .panel {{
                background: #161b22; border: 1px solid #30363d;
                border-radius: 6px; padding: 20px; margin-bottom: 20px;
            }}
            .failures-table {{ background: #161b22; border: 1px solid #30363d; border-radius: 6px; padding: 20px; margin-bottom: 20px; flex: 1; min-width: 300px; }}
            table {{ width: 100%; border-collapse: collapse; }}
            th, td {{ text-align: left; padding: 10px; border-bottom: 1px solid #21262d; }}
            th {{ color: #8b949e; font-weight: 600; font-size: 11px; text-transform: uppercase; }}
            tr:last-child td {{ border-bottom: none; }}
            .badge {{
                display: inline-block; padding: 2px 8px; border-radius: 4px;
                font-size: 12px; font-weight: 600;
            }}
            .badge.success {{ background: #3fb9501a; color: #3fb950; }}
            .badge.warning {{ background: #d299221a; color: #d29922; }}
            .badge.error {{ background: #f851491a; color: #f85149; }}
            .badge.info {{ background: #58a6ff1a; color: #58a6ff; }}

            .controls {{
                margin-bottom: 20px; display: flex;
                gap: 10px; align-items: center; flex-wrap: wrap;
            }}
            .btn {{
                padding: 8px 16px; border: none; border-radius: 6px;
                font-size: 13px; font-weight: 600; cursor: pointer;
                transition: opacity 0.2s;
            }}
            .btn:hover {{ opacity: 0.8; }}
            .btn-start {{ background: #3fb950; color: white; }}
            .btn-stop {{ background: #f85149; color: white; }}
            .fill-control {{
                display: flex; align-items: center; gap: 8px;
                background: #161b22; border: 1px solid #30363d;
                border-radius: 6px; padding: 6px 14px;
            }}
            .fill-control label {{
                color: #8b949e; font-size: 12px; font-weight: 600;
                text-transform: uppercase; white-space: nowrap;
            }}
            .fill-control input[type=range] {{ width: 140px; accent-color: #58a6ff; }}
            .fill-control .fill-value {{
                color: #58a6ff; font-weight: 700; font-size: 15px;
                min-width: 36px; text-align: right;
            }}
            .link-btn {{
                padding: 8px 16px; border: 1px solid #30363d; border-radius: 6px;
                font-size: 13px; font-weight: 600; cursor: pointer;
                background: #161b22; color: #58a6ff; text-decoration: none;
                transition: border-color 0.2s;
            }}
            .link-btn:hover {{ border-color: #58a6ff; }}

            .explorer-viewport {{
                position: relative; width: 100%; height: 400px;
                overflow: hidden; border-radius: 4px;
            }}
            .explorer-viewport iframe {{
                position: absolute; top: -385px; left: 0;
                width: 100%; height: calc(100% + 600px); border: none;
            }}

            /* WS Terminal */
            .ws-terminal-bar {{
                display: flex; gap: 8px; align-items: center;
                margin-bottom: 10px; flex-wrap: wrap;
            }}
            .ws-terminal-bar select, .ws-terminal-bar button {{
                background: #0d1117; color: #c9d1d9; border: 1px solid #30363d;
                border-radius: 4px; padding: 6px 10px; font-size: 13px;
            }}
            .ws-terminal-bar select {{ min-width: 120px; }}
            .ws-terminal-bar button {{ cursor: pointer; font-weight: 600; }}
            .ws-terminal-bar button:hover {{ border-color: #58a6ff; }}
            .ws-terminal-bar button.active {{ background: #3fb950; color: #0d1117; border-color: #3fb950; }}
            .stream-filters {{
                display: flex; gap: 6px; flex-wrap: wrap; align-items: center;
            }}
            .stream-filters label {{
                display: flex; align-items: center; gap: 4px;
                font-size: 12px; color: #8b949e; cursor: pointer;
                background: #0d1117; border: 1px solid #30363d;
                border-radius: 4px; padding: 4px 8px;
            }}
            .stream-filters label.checked {{
                border-color: #58a6ff; color: #58a6ff;
            }}
            .stream-filters input {{ display: none; }}
            #ws-output {{
                background: #010409; border: 1px solid #21262d; border-radius: 4px;
                height: 400px; overflow-y: auto; padding: 10px;
                font-family: 'SF Mono', 'Fira Code', 'Cascadia Code', monospace;
                font-size: 12px; line-height: 1.5;
                scroll-behavior: smooth;
            }}
            .ws-line {{ margin: 0; white-space: pre-wrap; word-break: break-all; }}
            .ws-line.ledger {{ color: #58a6ff; }}
            .ws-line.transaction {{ color: #3fb950; }}
            .ws-line.validation {{ color: #d29922; }}
            .ws-line.server {{ color: #8b949e; }}
            .ws-line.consensus {{ color: #bc8cff; }}
            .ws-line.peer {{ color: #f0883e; }}
            .ws-line.error {{ color: #f85149; }}
            .ws-line.info {{ color: #8b949e; font-style: italic; }}
            .ws-line.txn-Payment {{ color: #3fb950; }}
            .ws-line.txn-OfferCreate {{ color: #58a6ff; }}
            .ws-line.txn-OfferCancel {{ color: #6cb6ff; }}
            .ws-line.txn-TrustSet {{ color: #d29922; }}
            .ws-line.txn-AccountSet {{ color: #8b949e; }}
            .ws-line.txn-NFTokenMint {{ color: #bc8cff; }}
            .ws-line.txn-NFTokenBurn {{ color: #986ee2; }}
            .ws-line.txn-NFTokenCreateOffer {{ color: #a371f7; }}
            .ws-line.txn-NFTokenCancelOffer {{ color: #8957e5; }}
            .ws-line.txn-NFTokenAcceptOffer {{ color: #c297ff; }}
            .ws-line.txn-TicketCreate {{ color: #7ee787; }}
            .ws-line.txn-MPTokenIssuanceCreate {{ color: #f0883e; }}
            .ws-line.txn-MPTokenIssuanceSet {{ color: #d4762c; }}
            .ws-line.txn-MPTokenAuthorize {{ color: #e09b4f; }}
            .ws-line.txn-MPTokenIssuanceDestroy {{ color: #c45e1a; }}
            .ws-line.txn-AMMCreate {{ color: #f778ba; }}
            .ws-line.txn-AMMDeposit {{ color: #da70d6; }}
            .ws-line.txn-AMMWithdraw {{ color: #b8527a; }}
            .ws-line.txn-Batch {{ color: #79c0ff; }}
            .stream-filters label.txn-Payment {{ border-color: #3fb95066; }}
            .stream-filters label.txn-Payment.checked {{ border-color: #3fb950; color: #3fb950; }}
            .stream-filters label.txn-OfferCreate {{ border-color: #58a6ff66; }}
            .stream-filters label.txn-OfferCreate.checked {{ border-color: #58a6ff; color: #58a6ff; }}
            .stream-filters label.txn-OfferCancel {{ border-color: #6cb6ff66; }}
            .stream-filters label.txn-OfferCancel.checked {{ border-color: #6cb6ff; color: #6cb6ff; }}
            .stream-filters label.txn-TrustSet {{ border-color: #d2992266; }}
            .stream-filters label.txn-TrustSet.checked {{ border-color: #d29922; color: #d29922; }}
            .stream-filters label.txn-AccountSet {{ border-color: #8b949e66; }}
            .stream-filters label.txn-AccountSet.checked {{ border-color: #8b949e; color: #8b949e; }}
            .stream-filters label.txn-NFTokenMint {{ border-color: #bc8cff66; }}
            .stream-filters label.txn-NFTokenMint.checked {{ border-color: #bc8cff; color: #bc8cff; }}
            .stream-filters label.txn-NFTokenBurn {{ border-color: #986ee266; }}
            .stream-filters label.txn-NFTokenBurn.checked {{ border-color: #986ee2; color: #986ee2; }}
            .stream-filters label.txn-NFTokenCreateOffer {{ border-color: #a371f766; }}
            .stream-filters label.txn-NFTokenCreateOffer.checked {{ border-color: #a371f7; color: #a371f7; }}
            .stream-filters label.txn-NFTokenCancelOffer {{ border-color: #8957e566; }}
            .stream-filters label.txn-NFTokenCancelOffer.checked {{ border-color: #8957e5; color: #8957e5; }}
            .stream-filters label.txn-NFTokenAcceptOffer {{ border-color: #c297ff66; }}
            .stream-filters label.txn-NFTokenAcceptOffer.checked {{ border-color: #c297ff; color: #c297ff; }}
            .stream-filters label.txn-TicketCreate {{ border-color: #7ee78766; }}
            .stream-filters label.txn-TicketCreate.checked {{ border-color: #7ee787; color: #7ee787; }}
            .stream-filters label.txn-MPTokenIssuanceCreate {{ border-color: #f0883e66; }}
            .stream-filters label.txn-MPTokenIssuanceCreate.checked {{ border-color: #f0883e; color: #f0883e; }}
            .stream-filters label.txn-MPTokenIssuanceSet {{ border-color: #d4762c66; }}
            .stream-filters label.txn-MPTokenIssuanceSet.checked {{ border-color: #d4762c; color: #d4762c; }}
            .stream-filters label.txn-MPTokenAuthorize {{ border-color: #e09b4f66; }}
            .stream-filters label.txn-MPTokenAuthorize.checked {{ border-color: #e09b4f; color: #e09b4f; }}
            .stream-filters label.txn-MPTokenIssuanceDestroy {{ border-color: #c45e1a66; }}
            .stream-filters label.txn-MPTokenIssuanceDestroy.checked {{ border-color: #c45e1a; color: #c45e1a; }}
            .stream-filters label.txn-AMMCreate {{ border-color: #f778ba66; }}
            .stream-filters label.txn-AMMCreate.checked {{ border-color: #f778ba; color: #f778ba; }}
            .stream-filters label.txn-AMMDeposit {{ border-color: #da70d666; }}
            .stream-filters label.txn-AMMDeposit.checked {{ border-color: #da70d6; color: #da70d6; }}
            .stream-filters label.txn-AMMWithdraw {{ border-color: #b8527a66; }}
            .stream-filters label.txn-AMMWithdraw.checked {{ border-color: #b8527a; color: #b8527a; }}
            .stream-filters label.txn-Batch {{ border-color: #79c0ff66; }}
            .stream-filters label.txn-Batch.checked {{ border-color: #79c0ff; color: #79c0ff; }}
            /* Grouped txn type layout */
            .txn-type-groups {{ display: flex; gap: 12px; flex-wrap: wrap; align-items: flex-start; }}
            .txn-type-group {{ display: flex; flex-direction: column; gap: 4px; }}
            .txn-type-group-label {{ color: #8b949e; font-size: 10px; text-transform: uppercase; font-weight: 700; letter-spacing: 0.5px; margin-bottom: 2px; }}
            /* Transaction Control pane toggles */
            .txn-control-grid {{ display: flex; gap: 16px; flex-wrap: wrap; align-items: flex-start; }}
            .txn-control-group {{ display: flex; flex-direction: column; gap: 4px; }}
            .txn-control-group-label {{ color: #8b949e; font-size: 10px; text-transform: uppercase; font-weight: 700; letter-spacing: 0.5px; margin-bottom: 2px; }}
            .txn-toggle {{ display: inline-block; padding: 4px 10px; border-radius: 4px; font-size: 12px; font-weight: 600; cursor: pointer; border: 1px solid #30363d; background: #0d1117; color: #484f58; transition: all 0.15s ease; user-select: none; }}
            .txn-toggle.enabled {{ background: #161b22; }}
            .txn-toggle:hover {{ opacity: 0.85; }}
            .txn-toggle.config-disabled {{ opacity: 0.35; cursor: not-allowed; text-decoration: line-through; }}
            .txn-toggle.config-disabled:hover {{ opacity: 0.35; }}
            .txn-control-group-label {{ cursor: pointer; }}
            .txn-control-group-label:hover {{ color: #c9d1d9; }}
            .ws-status {{ font-size: 12px; margin-left: auto; }}
            .ws-status.connected {{ color: #3fb950; }}
            .ws-status.disconnected {{ color: #f85149; }}
            .msg-count {{ color: #8b949e; font-size: 12px; }}
        </style>
    </head>
    <body>
        <div class="container">
            <h1>Workload Dashboard</h1>
            <div class="subtitle" id="subtitle">Loading...</div>

            <div class="controls">
                <button class="btn btn-start" onclick="fetch('/workload/start', {{method:'POST'}})">Start</button>
                <button class="btn btn-stop" onclick="fetch('/workload/stop', {{method:'POST'}})">Stop</button>
                <a class="link-btn" href="https://custom.xrpl.org/localhost:6006" target="_blank">XRPL Explorer</a>
                <a class="link-btn" href="/logs/page" target="_blank">Logs</a>
                <a class="link-btn" href="/docs" target="_blank">API Docs</a>
            </div>

            <!-- Stats cards (updated via JS) -->
            <div class="stats-grid" id="fee-stats"></div>
            <div class="stats-grid" id="txn-stats"></div>

            <!-- Transaction Control -->
            <div class="panel">
                <h2>Transaction Control</h2>
                <div style="display:flex;align-items:center;gap:20px;margin-bottom:12px;flex-wrap:wrap">
                    <div class="fill-control" style="flex:1;min-width:280px">
                        <label>Target txns/ledger: <span id="target-txns-value" style="font-weight:700;color:#58a6ff">0</span></label>
                        <input type="range" id="target-txns-input" min="0" max="1000" step="10" value="100"
                               style="width:100%;accent-color:#58a6ff;cursor:pointer"
                               oninput="document.getElementById('target-txns-value').textContent=this.value"
                               onchange="fetch('/workload/target-txns',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{target_txns:parseInt(this.value)}})}})" >
                    </div>
                </div>
                <div class="txn-control-grid" id="txn-control-pane"></div>
            </div>

            <!-- Transaction types + failures side by side -->
            <div id="tables-container" style="display:flex;gap:20px;flex-wrap:wrap"></div>

            <!-- Explorer embed -->
            <div class="panel">
                <h2>Ledger Stream</h2>
                <div class="explorer-viewport">
                    <iframe src="https://custom.xrpl.org/localhost:6006" id="explorer-frame"></iframe>
                </div>
            </div>

            <!-- WS Terminal -->
            <div class="panel">
                <h2>Node WebSocket</h2>
                <div class="ws-terminal-bar">
                    <select id="ws-node"></select>
                    <button id="ws-connect-btn" onclick="toggleWs()">Connect</button>
                    <div class="stream-filters" id="stream-filters"></div>
                    <span class="msg-count" id="ws-msg-count"></span>
                    <span class="ws-status disconnected" id="ws-status">disconnected</span>
                </div>
                <div class="ws-terminal-bar" style="margin-top:0;align-items:flex-start">
                    <span style="color:#8b949e;font-size:11px;text-transform:uppercase;font-weight:600;padding-top:4px">Txn types</span>
                    <div class="txn-type-groups" id="txn-type-filters"></div>
                </div>
                <div id="ws-output"></div>
            </div>
        </div>

        <script>
        // --- Nodes ---
        const NODES = {nodes_json};
        const STREAMS = [
            {{name:'ledger', label:'Ledger', on:true}},
            {{name:'transactions', label:'Transactions', on:true}},
            {{name:'validations', label:'Validations', on:false}},
            {{name:'consensus', label:'Consensus', on:false}},
            {{name:'peer_status', label:'Peers', on:false}},
            {{name:'server', label:'Server', on:false}},
        ];
        let ws = null;
        let msgCount = 0;
        let activeStreams = new Set(STREAMS.filter(s=>s.on).map(s=>s.name));
        const MAX_LINES = 500;

        // Populate node dropdown
        const nodeSelect = document.getElementById('ws-node');
        NODES.forEach(n => {{
            const opt = document.createElement('option');
            opt.value = n.ws;
            opt.textContent = n.name + ' (:' + n.ws + ')';
            nodeSelect.appendChild(opt);
        }});

        // Populate stream filter checkboxes
        const filtersEl = document.getElementById('stream-filters');
        STREAMS.forEach(s => {{
            const lbl = document.createElement('label');
            lbl.className = s.on ? 'checked' : '';
            const cb = document.createElement('input');
            cb.type = 'checkbox';
            cb.checked = s.on;
            cb.dataset.stream = s.name;
            cb.onchange = function() {{
                if (this.checked) activeStreams.add(s.name);
                else activeStreams.delete(s.name);
                lbl.className = this.checked ? 'checked' : '';
                if (ws && ws.readyState === WebSocket.OPEN) resubscribe();
            }};
            lbl.appendChild(cb);
            lbl.appendChild(document.createTextNode(s.label));
            filtersEl.appendChild(lbl);
        }});

        // Shared txn type definitions
        const TXN_TYPE_GROUPS = [
            {{label: 'Core', types: ['Payment','TrustSet','AccountSet','TicketCreate']}},
            {{label: 'DEX', types: ['OfferCreate','OfferCancel']}},
            {{label: 'AMM', types: ['AMMCreate','AMMDeposit','AMMWithdraw']}},
            {{label: 'NFT', types: ['NFTokenMint','NFTokenBurn','NFTokenCreateOffer','NFTokenCancelOffer','NFTokenAcceptOffer']}},
            {{label: 'MPT', types: ['MPTokenIssuanceCreate','MPTokenIssuanceSet','MPTokenAuthorize','MPTokenIssuanceDestroy']}},
            {{label: 'Other', types: ['Batch']}},
        ];
        const TXN_TYPES = TXN_TYPE_GROUPS.flatMap(g => g.types);
        const TXN_COLORS = {{
            Payment:'#3fb950', TrustSet:'#d29922', AccountSet:'#8b949e', TicketCreate:'#7ee787',
            OfferCreate:'#58a6ff', OfferCancel:'#6cb6ff',
            AMMCreate:'#f778ba', AMMDeposit:'#da70d6', AMMWithdraw:'#b8527a',
            NFTokenMint:'#bc8cff', NFTokenBurn:'#986ee2', NFTokenCreateOffer:'#a371f7',
            NFTokenCancelOffer:'#8957e5', NFTokenAcceptOffer:'#c297ff',
            MPTokenIssuanceCreate:'#f0883e', MPTokenIssuanceSet:'#d4762c',
            MPTokenAuthorize:'#e09b4f', MPTokenIssuanceDestroy:'#c45e1a',
            Batch:'#79c0ff',
        }};

        // WS terminal display filters (client-side only)
        let activeTxnTypes = new Set(TXN_TYPES);
        const txnFiltersEl = document.getElementById('txn-type-filters');
        TXN_TYPE_GROUPS.forEach(group => {{
            const groupDiv = document.createElement('div');
            groupDiv.className = 'txn-type-group';
            const groupLabel = document.createElement('div');
            groupLabel.className = 'txn-type-group-label';
            groupLabel.textContent = group.label;
            groupDiv.appendChild(groupLabel);
            group.types.forEach(tt => {{
                const lbl = document.createElement('label');
                lbl.className = 'checked txn-' + tt;
                const cb = document.createElement('input');
                cb.type = 'checkbox';
                cb.checked = true;
                cb.onchange = function() {{
                    if (this.checked) activeTxnTypes.add(tt);
                    else activeTxnTypes.delete(tt);
                    lbl.className = (this.checked ? 'checked ' : '') + 'txn-' + tt;
                }};
                lbl.appendChild(cb);
                lbl.appendChild(document.createTextNode(tt));
                groupDiv.appendChild(lbl);
            }});
            txnFiltersEl.appendChild(groupDiv);
        }});

        // Transaction Control pane (controls actual generation)
        const txnControlEl = document.getElementById('txn-control-pane');
        let enabledGenTypes = new Set(TXN_TYPES);
        let configDisabledTypes = new Set();

        function renderTxnControl() {{
            txnControlEl.innerHTML = '';
            TXN_TYPE_GROUPS.forEach(group => {{
                const groupDiv = document.createElement('div');
                groupDiv.className = 'txn-control-group';
                const groupLabel = document.createElement('div');
                groupLabel.className = 'txn-control-group-label';
                groupLabel.textContent = group.label;
                // Group toggle: click label to toggle all toggleable types in group
                const toggleable = group.types.filter(t => !configDisabledTypes.has(t));
                if (toggleable.length > 0) {{
                    groupLabel.onclick = async () => {{
                        const anyOn = toggleable.some(t => enabledGenTypes.has(t));
                        const newEnabled = !anyOn;
                        for (const tt of toggleable) {{
                            try {{
                                await fetch('/workload/toggle-type', {{
                                    method: 'POST',
                                    headers: {{'Content-Type': 'application/json'}},
                                    body: JSON.stringify({{txn_type: tt, enabled: newEnabled}}),
                                }});
                                if (newEnabled) enabledGenTypes.add(tt);
                                else enabledGenTypes.delete(tt);
                            }} catch(e) {{ console.error('Group toggle failed:', e); }}
                        }}
                        renderTxnControl();
                    }};
                }}
                groupDiv.appendChild(groupLabel);
                group.types.forEach(tt => {{
                    const btn = document.createElement('div');
                    const isConfigDisabled = configDisabledTypes.has(tt);
                    const isOn = enabledGenTypes.has(tt);
                    btn.className = 'txn-toggle' + (isOn ? ' enabled' : '') + (isConfigDisabled ? ' config-disabled' : '');
                    btn.textContent = tt;
                    const color = TXN_COLORS[tt] || '#8b949e';
                    if (isConfigDisabled) {{
                        btn.title = tt + ' — disabled in config.toml (amendment not available)';
                    }} else {{
                        if (isOn) {{ btn.style.borderColor = color; btn.style.color = color; }}
                        btn.onclick = async () => {{
                            const newEnabled = !enabledGenTypes.has(tt);
                            try {{
                                await fetch('/workload/toggle-type', {{
                                    method: 'POST',
                                    headers: {{'Content-Type': 'application/json'}},
                                    body: JSON.stringify({{txn_type: tt, enabled: newEnabled}}),
                                }});
                                if (newEnabled) enabledGenTypes.add(tt);
                                else enabledGenTypes.delete(tt);
                                renderTxnControl();
                            }} catch(e) {{ console.error('Toggle failed:', e); }}
                        }};
                    }}
                    groupDiv.appendChild(btn);
                }});
                txnControlEl.appendChild(groupDiv);
            }});
        }}

        async function refreshDisabledTypes() {{
            try {{
                const res = await fetch('/workload/disabled-types');
                const data = await res.json();
                enabledGenTypes = new Set(data.enabled_types);
                configDisabledTypes = new Set(data.config_disabled || []);
                renderTxnControl();
            }} catch(e) {{ /* workload not ready yet */ }}
        }}
        refreshDisabledTypes();
        setInterval(refreshDisabledTypes, 10000);

        function wsLog(text, cls) {{
            const out = document.getElementById('ws-output');
            const line = document.createElement('div');
            line.className = 'ws-line ' + (cls || '');
            const ts = new Date().toLocaleTimeString('en-US', {{hour12:false}});
            line.textContent = ts + '  ' + text;
            out.appendChild(line);
            while (out.children.length > MAX_LINES) out.removeChild(out.firstChild);
            out.scrollTop = out.scrollHeight;
        }}

        // Returns [text, cssClass, txnType|null]
        function formatMsg(data) {{
            const t = data.type || '';
            if (t === 'ledgerClosed') {{
                return [
                    `LEDGER #${{data.ledger_index}}  txns=${{data.txn_count}}  close=${{data.ledger_time}}`,
                    'ledger', null
                ];
            }}
            if (t === 'transaction') {{
                const tx = data.transaction || {{}};
                const tt = tx.TransactionType || '?';
                const v = data.validated ? 'validated' : 'proposed';
                const r = data.engine_result || data.meta?.TransactionResult || '';
                return [
                    `${{tt}}  ${{tx.Account?.slice(0,12)}}..  ${{r}}  [${{v}}]  ${{tx.hash?.slice(0,16)}}..`,
                    'txn-' + tt, tt
                ];
            }}
            if (t === 'validationReceived') {{
                return [
                    `VALIDATION  ledger=${{data.ledger_index}}  key=${{data.validation_public_key?.slice(0,16)}}..`,
                    'validation', null
                ];
            }}
            if (t === 'serverStatus') {{
                return [
                    `SERVER  load=${{data.load_factor}}  state=${{data.server_status}}`,
                    'server', null
                ];
            }}
            if (t === 'consensusPhase') {{
                return [`CONSENSUS  phase=${{data.consensus}}`, 'consensus', null];
            }}
            if (t === 'peerStatusChange') {{
                return [`PEER  ${{data.action}}  ${{data.address || ''}}`, 'peer', null];
            }}
            return [JSON.stringify(data).slice(0, 200), '', null];
        }}

        function resubscribe() {{
            if (!ws || ws.readyState !== WebSocket.OPEN) return;
            // Unsubscribe all, then resubscribe active
            ws.send(JSON.stringify({{command:'unsubscribe', streams:STREAMS.map(s=>s.name)}}));
            const streams = Array.from(activeStreams);
            if (streams.length > 0) {{
                ws.send(JSON.stringify({{command:'subscribe', streams}}));
                wsLog('Subscribed: ' + streams.join(', '), 'info');
            }}
        }}

        function toggleWs() {{
            const btn = document.getElementById('ws-connect-btn');
            const statusEl = document.getElementById('ws-status');
            if (ws && ws.readyState <= WebSocket.OPEN) {{
                ws.close();
                return;
            }}
            const port = nodeSelect.value;
            const url = 'ws://{hostname}:' + port;
            wsLog('Connecting to ' + url + '...', 'info');
            ws = new WebSocket(url);
            ws.onopen = () => {{
                statusEl.textContent = 'connected';
                statusEl.className = 'ws-status connected';
                btn.textContent = 'Disconnect';
                btn.className = 'active';
                const streams = Array.from(activeStreams);
                ws.send(JSON.stringify({{command:'subscribe', streams}}));
                wsLog('Connected. Subscribed: ' + streams.join(', '), 'info');
            }};
            ws.onmessage = (ev) => {{
                const data = JSON.parse(ev.data);
                if (data.type === 'response') return; // subscribe ack
                const [text, cls, txnType] = formatMsg(data);
                // Filter: if it's a transaction, check txn type filter
                if (txnType && !activeTxnTypes.has(txnType)) return;
                wsLog(text, cls);
                msgCount++;
                document.getElementById('ws-msg-count').textContent = msgCount + ' msgs';
            }};
            ws.onclose = () => {{
                statusEl.textContent = 'disconnected';
                statusEl.className = 'ws-status disconnected';
                btn.textContent = 'Connect';
                btn.className = '';
                wsLog('Disconnected', 'info');
            }};
            ws.onerror = () => wsLog('WebSocket error', 'error');
        }}

        // --- Stats polling (no page reload, keeps WS alive) ---
        function fmt(n) {{ return n == null ? '—' : Number(n).toLocaleString(); }}
        function pct(a, b) {{ return b > 0 ? (a/b*100).toFixed(1) : '0.0'; }}
        function fmtUptime(sec) {{
            if (!sec) return '—';
            const d = Math.floor(sec / 86400), h = Math.floor(sec % 86400 / 3600),
                  m = Math.floor(sec % 3600 / 60), s = sec % 60;
            if (d > 0) return d + 'd ' + h + 'h ' + m + 'm';
            if (h > 0) return h + 'h ' + m + 'm ' + s + 's';
            if (m > 0) return m + 'm ' + s + 's';
            return s + 's';
        }}

        function statCard(label, value, cls, extra, barPct) {{
            let html = '<div class="stat-card"><div class="stat-label">' + label + '</div>';
            html += '<div class="stat-value ' + (cls||'') + '">' + value + '</div>';
            if (extra) html += '<div class="stat-percentage">' + extra + '</div>';
            if (barPct != null) {{
                html += '<div class="progress-bar"><div class="progress-fill ' + (cls||'') + '" style="width:' + barPct + '%"></div></div>';
            }}
            return html + '</div>';
        }}

        async function refreshStats() {{
            try {{
                const [statsRes, feeRes, ttRes, failedRes] = await Promise.all([
                    fetch('/state/summary').then(r=>r.json()),
                    fetch('/state/fees').then(r=>r.json()),
                    fetch('/workload/target-txns').then(r=>r.json()),
                    fetch('/state/failed').then(r=>r.json()),
                ]);
                const s = statsRes;
                const f = feeRes;
                const bs = s.by_state || {{}};
                const total = s.total_tracked || 0;
                const validated = bs.VALIDATED || 0;
                const rejected = bs.REJECTED || 0;
                const submitted = bs.SUBMITTED || 0;
                const created = bs.CREATED || 0;
                const retryable = bs.RETRYABLE || 0;
                const expired = bs.EXPIRED || 0;

                // Subtitle
                document.getElementById('subtitle').innerHTML =
                    'Live monitoring &bull; Ledger ' + f.ledger_current_index + ' @ {hostname}' +
                    ' &bull; ' + fmt(s.ledgers_elapsed) + ' ledgers (' + fmtUptime(s.uptime_seconds) + ')';

                // Target txns slider (don't overwrite while user is dragging)
                const ttInput = document.getElementById('target-txns-input');
                if (document.activeElement !== ttInput) {{
                    ttInput.value = ttRes.target_txns_per_ledger;
                }}
                document.getElementById('target-txns-value').textContent = ttRes.target_txns_per_ledger;

                // Fee stats
                const feeWarn = f.minimum_fee > f.base_fee ? 'warning' : 'success';
                const qPct = f.max_queue_size > 0 ? (f.current_queue_size/f.max_queue_size*100) : 0;
                const lastClosed = f.last_closed_txn_count || 0;
                const lPct = f.expected_ledger_size > 0 ? (lastClosed/f.expected_ledger_size*100) : 0;
                document.getElementById('fee-stats').innerHTML =
                    statCard('Fee (min/open/base)', f.minimum_fee+'/'+f.open_ledger_fee+'/'+f.base_fee, feeWarn, 'drops') +
                    statCard('Queue Utilization', f.current_queue_size+'/'+f.max_queue_size, 'info', qPct.toFixed(1)+'%', qPct) +
                    statCard('Ledger Utilization', lastClosed+'/'+f.expected_ledger_size, 'info', lPct.toFixed(1)+'%', lPct);

                // Txn stats
                document.getElementById('txn-stats').innerHTML =
                    statCard('Total Transactions', fmt(total), 'info') +
                    statCard('Validated', fmt(validated), 'success', pct(validated,total)+'%', pct(validated,total)) +
                    statCard('Rejected', fmt(rejected), 'error', pct(rejected,total)+'%', pct(rejected,total)) +
                    statCard('In-Flight', fmt(submitted+created), 'warning', 'Submitted: '+submitted+' | Created: '+created) +
                    statCard('Expired', fmt(expired), '');

                // Tables
                let tablesHtml = '';

                // Transaction type breakdown (left)
                const byTypeTotal = s.by_type_total || {{}};
                const byTypeValidated = s.by_type_validated || {{}};
                const typePct = (e) => e[1] > 0 ? (byTypeValidated[e[0]]||0)/e[1] : -1;
                const sortedTypes = Object.entries(byTypeTotal).sort((a,b) => typePct(b)-typePct(a) || b[1]-a[1]);
                if (sortedTypes.length) {{
                    tablesHtml += '<div class="failures-table"><h2>Transaction Types ('+sortedTypes.length+')</h2><table><thead><tr><th>Type</th><th>Success Rate</th></tr></thead><tbody>';
                    sortedTypes.forEach(([t,total]) => {{
                        const validated = byTypeValidated[t] || 0;
                        const pct = total > 0 ? Math.round(validated/total*100) : 0;
                        const color = pct >= 80 ? '#3fb950' : pct >= 50 ? '#d29922' : '#f85149';
                        const disabled = configDisabledTypes.has(t);
                        const amendmentDisabled = !disabled && total > 0 && validated === 0;
                        const link = '<a href="/state/type/'+t+'/page" target="_blank" style="text-decoration:none;color:inherit">'+t+'</a>';
                        const nameHtml = disabled
                            ? '<s style="color:#484f58">'+link+'</s> <span style="color:#484f58;font-size:11px">disabled</span>'
                            : amendmentDisabled
                            ? '<span style="opacity:0.4">'+link+'</span> <span style="color:#484f58;font-size:11px">temDISABLED</span>'
                            : link;
                        tablesHtml += '<tr><td>'+nameHtml+'</td><td><span style="color:'+color+';font-weight:600">'+fmt(validated)+'/'+fmt(total)+' ('+pct+'%)</span></td></tr>';
                    }});
                    tablesHtml += '</tbody></table></div>';
                }}

                // Top failures (right)
                const INTERNAL = new Set(['CASCADE_EXPIRED','unknown','']);
                const failMap = {{}};
                (failedRes.failed||[]).forEach(f => {{
                    const r = f.engine_result_final || f.engine_result_first || 'unknown';
                    if (!INTERNAL.has(r) && !r.startsWith('tes')) failMap[r] = (failMap[r]||0) + 1;
                }});
                const topFail = Object.entries(failMap).sort((a,b)=>b[1]-a[1]).slice(0,10);
                if (topFail.length) {{
                    tablesHtml += '<div class="failures-table"><h2>Top Failures</h2><table><thead><tr><th>Error Code</th><th>Count</th></tr></thead><tbody>';
                    topFail.forEach(([r,c]) => {{
                        tablesHtml += '<tr><td><a href="/state/failed/'+r+'/page" target="_blank" style="text-decoration:none"><span class="badge error" style="cursor:pointer">'+r+'</span></a></td><td>'+fmt(c)+'</td></tr>';
                    }});
                    tablesHtml += '</tbody></table></div>';
                }}

                document.getElementById('tables-container').innerHTML = tablesHtml;

            }} catch(e) {{
                console.error('Stats refresh error:', e);
            }}
        }}

        // Initial load + periodic refresh
        refreshStats();
        setInterval(refreshStats, 3000);

</script>
    </body>
    </html>
    """

    return HTMLResponse(content=html_content)


@r_state.get("/pending")
async def state_pending():
    return {"pending": app.state.workload.snapshot_pending()}


@r_state.get("/failed")
async def state_failed():
    return {"failed": app.state.workload.snapshot_failed()}


@r_state.get("/failed/{error_code}")
async def state_failed_by_code(error_code: str):
    """Get failed transactions filtered by engine result code."""
    all_failed = app.state.workload.snapshot_failed()
    filtered = [
        f
        for f in all_failed
        if f.get("engine_result_final") == error_code or f.get("engine_result_first") == error_code
    ]
    return {"error_code": error_code, "count": len(filtered), "transactions": filtered}


@r_state.get("/failed/{error_code}/page")
async def state_failed_page(error_code: str):
    """HTML page showing failed transactions for a specific error code."""
    all_failed = app.state.workload.snapshot_failed()
    filtered = [
        f
        for f in all_failed
        if f.get("engine_result_final") == error_code or f.get("engine_result_first") == error_code
    ]

    hostname = RPC.split("//")[1].split(":")[0] if "//" in RPC else RPC.split(":")[0]
    explorer_base = f"https://custom.xrpl.org/{hostname}:6006"
    # tec codes are applied to the ledger and have an on-chain hash
    on_ledger = error_code.startswith("tec") or error_code.startswith("tes")
    rows = ""
    for f in filtered:
        account = f.get("account", "")
        tx_hash = f.get("tx_hash", "")
        account_cell = (
            f'<a href="{explorer_base}/accounts/{account}" target="_blank"><code>{account}</code></a>'
            if account
            else ""
        )
        if on_ledger and tx_hash:
            hash_cell = f'<a href="{explorer_base}/transactions/{tx_hash}" target="_blank"><code>{tx_hash}</code></a>'
        else:
            hash_cell = f"<code>{tx_hash}</code>"
        msg = f.get("engine_result_message") or ""
        rows += (
            f"<tr>"
            f"<td>{hash_cell}</td>"
            f"<td>{f.get('transaction_type', '')}</td>"
            f"<td>{account_cell}</td>"
            f"<td>{f.get('sequence', '')}</td>"
            f"<td>{f.get('state', '')}</td>"
            f"<td>{f.get('created_ledger', '')}</td>"
            f"<td>{f.get('last_ledger_seq', '')}</td>"
            f"<td style='font-size:11px;color:#8b949e'>{msg}</td>"
            f"</tr>"
        )

    html = f"""<!DOCTYPE html>
    <html><head>
    <title>Failed: {error_code}</title>
    <style>
        body {{ background: #0d1117; color: #c9d1d9; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', monospace; padding: 20px; }}
        h1 {{ color: #f85149; }}
        a {{ color: #58a6ff; }}
        table {{ border-collapse: collapse; width: 100%; margin-top: 16px; }}
        th, td {{ padding: 8px 12px; border: 1px solid #30363d; text-align: left; font-size: 13px; }}
        th {{ background: #161b22; color: #8b949e; text-transform: uppercase; font-size: 11px; cursor: pointer; user-select: none; }}
        th:hover {{ color: #c9d1d9; }}
        th .sort-arrow {{ font-size: 9px; margin-left: 4px; }}
        tr:hover {{ background: #161b22; }}
        code {{ color: #58a6ff; }}
        .badge {{ padding: 2px 8px; border-radius: 4px; font-size: 12px; font-weight: 600; background: #3d1f1f; color: #f85149; }}
        .count {{ color: #8b949e; font-size: 14px; }}
    </style>
    </head><body>
    <a href="/state/dashboard">&larr; Dashboard</a>
    <h1><span class="badge">{error_code}</span> <span class="count">{len(filtered)} transactions</span></h1>
    <table id="results-table">
        <thead><tr>
            <th data-col="0">Hash <span class="sort-arrow"></span></th>
            <th data-col="1">Type <span class="sort-arrow"></span></th>
            <th data-col="2">Account <span class="sort-arrow"></span></th>
            <th data-col="3" data-type="num">Seq <span class="sort-arrow"></span></th>
            <th data-col="4">State <span class="sort-arrow"></span></th>
            <th data-col="5" data-type="num">Created Ledger <span class="sort-arrow"></span></th>
            <th data-col="6" data-type="num">Last Ledger Seq <span class="sort-arrow"></span></th>
            <th data-col="7">Message <span class="sort-arrow"></span></th>
        </tr></thead>
        <tbody>{rows if rows else '<tr><td colspan="8" style="text-align:center;color:#8b949e">No transactions found</td></tr>'}</tbody>
    </table>
    <p style="margin-top:16px;color:#8b949e">JSON: <a href="/state/failed/{error_code}">/state/failed/{error_code}</a></p>
    <script>
    (function() {{
        const table = document.getElementById('results-table');
        const headers = table.querySelectorAll('th');
        let sortCol = -1, sortAsc = true;

        headers.forEach(th => {{
            th.addEventListener('click', () => {{
                const col = parseInt(th.dataset.col);
                const isNum = th.dataset.type === 'num';
                if (sortCol === col) {{ sortAsc = !sortAsc; }}
                else {{ sortCol = col; sortAsc = true; }}

                const tbody = table.querySelector('tbody');
                const rows = Array.from(tbody.querySelectorAll('tr'));
                rows.sort((a, b) => {{
                    const aText = a.cells[col]?.textContent.trim() || '';
                    const bText = b.cells[col]?.textContent.trim() || '';
                    if (isNum) {{
                        const aNum = parseInt(aText) || 0;
                        const bNum = parseInt(bText) || 0;
                        return sortAsc ? aNum - bNum : bNum - aNum;
                    }}
                    return sortAsc ? aText.localeCompare(bText) : bText.localeCompare(aText);
                }});
                rows.forEach(r => tbody.appendChild(r));

                headers.forEach(h => h.querySelector('.sort-arrow').textContent = '');
                th.querySelector('.sort-arrow').textContent = sortAsc ? ' ▲' : ' ▼';
            }});
        }});
    }})();
    </script>
    </body></html>"""
    return HTMLResponse(content=html)


@r_state.get("/expired")
async def state_expired():
    """Get transactions that expired without validating."""
    wl = app.state.workload
    expired = [r for r in wl.snapshot_pending(open_only=False) if r["state"] == "EXPIRED"]
    return {"expired": expired}


@r_state.get("/type/{txn_type}")
async def state_type_json(txn_type: str):
    """Get transactions filtered by transaction type."""
    wl = app.state.workload
    filtered = [r for r in wl.snapshot_pending(open_only=False) if r.get("transaction_type") == txn_type]
    return {"transaction_type": txn_type, "count": len(filtered), "transactions": filtered}


@r_state.get("/type/{txn_type}/page")
async def state_type_page(txn_type: str):
    """HTML page showing transactions for a specific type."""
    wl = app.state.workload
    filtered = [r for r in wl.snapshot_pending(open_only=False) if r.get("transaction_type") == txn_type]

    hostname = RPC.split("//")[1].split(":")[0] if "//" in RPC else RPC.split(":")[0]
    explorer_base = f"https://custom.xrpl.org/{hostname}:6006"
    rows = ""
    for f in filtered:
        account = f.get("account", "")
        tx_hash = f.get("tx_hash", "")
        state = f.get("state", "")
        account_cell = (
            f'<a href="{explorer_base}/accounts/{account}" target="_blank"><code>{account}</code></a>'
            if account
            else ""
        )
        on_ledger = state == "VALIDATED" or f.get("engine_result_final", "").startswith("tec")
        hash_cell = (
            f'<a href="{explorer_base}/transactions/{tx_hash}" target="_blank"><code>{tx_hash}</code></a>'
            if on_ledger and tx_hash
            else f"<code>{tx_hash}</code>"
        )
        er = f.get("engine_result_final") or f.get("engine_result_first") or ""
        state_cls = "success" if state == "VALIDATED" else "error" if state in ("REJECTED", "EXPIRED") else "warning"
        msg = f.get("engine_result_message") or ""
        rows += (
            f"<tr>"
            f"<td>{hash_cell}</td>"
            f"<td>{account_cell}</td>"
            f"<td>{f.get('sequence', '')}</td>"
            f"<td><span class='badge {state_cls}'>{state}</span></td>"
            f"<td><span class='badge'>{er}</span></td>"
            f"<td>{f.get('created_ledger', '')}</td>"
            f"<td>{f.get('validated_ledger') or ''}</td>"
            f"<td style='font-size:11px;color:#8b949e'>{msg}</td>"
            f"</tr>"
        )

    html = f"""<!DOCTYPE html>
    <html><head>
    <title>{txn_type}</title>
    <style>
        body {{ background: #0d1117; color: #c9d1d9; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', monospace; padding: 20px; }}
        h1 {{ color: #58a6ff; }}
        a {{ color: #58a6ff; }}
        table {{ border-collapse: collapse; width: 100%; margin-top: 16px; }}
        th, td {{ padding: 8px 12px; border: 1px solid #30363d; text-align: left; font-size: 13px; }}
        th {{ background: #161b22; color: #8b949e; text-transform: uppercase; font-size: 11px; cursor: pointer; user-select: none; }}
        th:hover {{ color: #c9d1d9; }}
        th .sort-arrow {{ font-size: 9px; margin-left: 4px; }}
        tr:hover {{ background: #161b22; }}
        code {{ color: #58a6ff; }}
        .badge {{ padding: 2px 8px; border-radius: 4px; font-size: 12px; font-weight: 600; }}
        .badge.success {{ background: #1f3d2a; color: #3fb950; }}
        .badge.error {{ background: #3d1f1f; color: #f85149; }}
        .badge.warning {{ background: #3d2f1f; color: #d29922; }}
        .count {{ color: #8b949e; font-size: 14px; }}
    </style>
    </head><body>
    <a href="/state/dashboard">&larr; Dashboard</a>
    <h1>{txn_type} <span class="count">{len(filtered)} transactions</span></h1>
    <table id="results-table">
        <thead><tr>
            <th data-col="0">Hash <span class="sort-arrow"></span></th>
            <th data-col="1">Account <span class="sort-arrow"></span></th>
            <th data-col="2" data-type="num">Seq <span class="sort-arrow"></span></th>
            <th data-col="3">State <span class="sort-arrow"></span></th>
            <th data-col="4">Result <span class="sort-arrow"></span></th>
            <th data-col="5" data-type="num">Created Ledger <span class="sort-arrow"></span></th>
            <th data-col="6" data-type="num">Validated Ledger <span class="sort-arrow"></span></th>
            <th data-col="7">Message <span class="sort-arrow"></span></th>
        </tr></thead>
        <tbody>{rows if rows else '<tr><td colspan="8" style="text-align:center;color:#8b949e">No transactions found</td></tr>'}</tbody>
    </table>
    <p style="margin-top:16px;color:#8b949e">JSON: <a href="/state/type/{txn_type}">/state/type/{txn_type}</a></p>
    <script>
    (function() {{{{
        const table = document.getElementById('results-table');
        const headers = table.querySelectorAll('th');
        let sortCol = -1, sortAsc = true;

        headers.forEach(th => {{{{
            th.addEventListener('click', () => {{{{
                const col = parseInt(th.dataset.col);
                const isNum = th.dataset.type === 'num';
                if (sortCol === col) {{{{ sortAsc = !sortAsc; }}}}
                else {{{{ sortCol = col; sortAsc = true; }}}}

                const tbody = table.querySelector('tbody');
                const rows = Array.from(tbody.querySelectorAll('tr'));
                rows.sort((a, b) => {{{{
                    const aText = a.cells[col]?.textContent.trim() || '';
                    const bText = b.cells[col]?.textContent.trim() || '';
                    if (isNum) {{{{
                        const aNum = parseInt(aText) || 0;
                        const bNum = parseInt(bText) || 0;
                        return sortAsc ? aNum - bNum : bNum - aNum;
                    }}}}
                    return sortAsc ? aText.localeCompare(bText) : bText.localeCompare(aText);
                }}}});
                rows.forEach(r => tbody.appendChild(r));

                headers.forEach(h => h.querySelector('.sort-arrow').textContent = '');
                th.querySelector('.sort-arrow').textContent = sortAsc ? ' ▲' : ' ▼';
            }}}});
        }}}});
    }}}})();
    </script>
    </body></html>"""
    return HTMLResponse(content=html)


@r_state.get("/tx/{tx_hash}")
async def state_tx(tx_hash: str):
    data = app.state.workload.snapshot_tx(tx_hash)
    if not data:
        raise HTTPException(404, "tx not tracked")
    return data


@r_state.get("/fees")
async def state_fees():
    """Get current fee escalation state from rippled."""
    wl = app.state.workload
    fee_info = await wl.get_fee_info()
    return {
        "expected_ledger_size": fee_info.expected_ledger_size,
        "current_ledger_size": fee_info.current_ledger_size,
        "current_queue_size": fee_info.current_queue_size,
        "max_queue_size": fee_info.max_queue_size,
        "base_fee": fee_info.base_fee,
        "minimum_fee": fee_info.minimum_fee,
        "median_fee": fee_info.median_fee,
        "open_ledger_fee": fee_info.open_ledger_fee,
        "ledger_current_index": fee_info.ledger_current_index,
        "queue_utilization": f"{fee_info.current_queue_size}/{fee_info.max_queue_size}",
        "ledger_utilization": f"{fee_info.current_ledger_size}/{fee_info.expected_ledger_size}",
        "last_closed_txn_count": wl.last_closed_ledger_txn_count,
    }


@r_state.get("/accounts")
async def state_accounts():
    wl = app.state.workload
    return {
        "count": len(wl.accounts),
        "addresses": list(wl.accounts.keys()),
    }


@r_state.get("/validations")
async def state_validations(limit: int = 100):
    """
    Return recent validation records from the in-memory store.
    Parameters
    ----------
    limit:
        Optional maximum number of records to return (default 100).
    """
    vals = list(app.state.workload.store.validations)[-limit:]
    return [{"txn": v.txn, "ledger": v.seq, "source": v.src} for v in reversed(vals)]


@r_state.get("/wallets")
def api_state_wallets():
    ws = app.state.workload.wallets
    return {"count": len(ws), "addresses": list(ws.keys())}


@r_state.get("/users")
def api_state_users():
    """Get all user wallets with addresses and seeds."""
    wl = app.state.workload
    users = [
        {
            "address": user.address,
            "seed": user.seed,
        }
        for user in wl.users
    ]
    return {"count": len(users), "users": users}


@r_state.get("/gateways")
def api_state_gateways():
    """Get all gateway wallets with addresses, seeds, and issued currencies."""
    wl = app.state.workload
    gateways = []
    for gateway in wl.gateways:
        issued_currencies = [curr.currency for curr in wl._currencies if curr.issuer == gateway.address]
        gateways.append(
            {
                "address": gateway.address,
                "seed": gateway.seed,
                "currencies": issued_currencies,
            }
        )
    return {"count": len(gateways), "gateways": gateways}


@r_state.get("/currencies")
def get_currencies():
    """Get all configured/issued currencies."""
    wl = app.state.workload
    currencies = [
        {
            "currency": curr.currency,
            "issuer": curr.issuer,
        }
        for curr in wl._currencies
    ]
    return {"count": len(currencies), "currencies": currencies}


@r_state.get("/mptokens")
def get_mptokens():
    """Get all tracked MPToken issuance IDs."""
    wl = app.state.workload
    mptoken_ids = getattr(wl, "_mptoken_issuance_ids", [])
    return {
        "count": len(mptoken_ids),
        "mptoken_issuance_ids": mptoken_ids,
        "note": "MPToken IDs are tracked automatically when MPTokenIssuanceCreate transactions validate",
    }


@r_state.get("/finality")
async def check_finality():
    """Manually trigger finality check for all pending submitted transactions."""
    wl = app.state.workload
    results = []

    for p in wl.find_by_state(C.TxState.SUBMITTED):
        try:
            state = await wl.check_finality(p)
            results.append(
                {
                    "tx_hash": p.tx_hash,
                    "state": state.name,
                    "ledger_index": p.validated_ledger,  # Get from PendingTx object
                }
            )
        except Exception as e:
            results.append(
                {
                    "tx_hash": p.tx_hash,
                    "error": str(e),
                }
            )

    return {
        "checked": len(results),
        "results": results,
    }


@r_state.get("/ws/stats")
async def ws_stats():
    """Return stats about WebSocket event processing."""
    queue_size = app.state.ws_queue.qsize()
    store_stats = app.state.workload.store.snapshot_stats()

    return {
        "queue_size": queue_size,
        "queue_maxsize": app.state.ws_queue.maxsize,
        "validations_by_source": store_stats.get("validated_by_source", {}),
        "recent_validations_count": store_stats.get("recent_validations", 0),
        "ws_event_counters": get_ws_counters(),
    }


@r_state.get("/diagnostics")
async def diagnostics():
    """Return diagnostic data about pending txn and account health."""
    wl = app.state.workload
    return wl.diagnostics_snapshot()


workload_running = False
workload_stop_event = None
workload_task = None
workload_stats = {"submitted": 0, "validated": 0, "failed": 0, "started_at": None}


async def continuous_workload():
    """Continuously build and submit transactions as fast as possible.

    No queue, no producer-consumer split. Single loop that:
    1. Finds free accounts (no in-flight txns)
    2. Builds + signs txns for up to `target_txns_per_ledger` accounts
    3. Submits them all in parallel via TaskGroup
    4. Immediately loops back — no waiting for ledger close

    The ledger close is the tick for *validation tracking*, not for submission.
    Real-world users submit whenever they want; so do we.

    Sequence safety: an account is only picked when pending_count == 0.
    record_created() marks it pending at build time, preventing double-allocation.
    """
    from workload.randoms import random
    from workload.txn_factory.builder import build_txn_dict, pick_eligible_txn_type, txn_model_cls

    global workload_stats
    wl = app.state.workload

    log.debug("Continuous workload started")
    workload_stats["started_at"] = perf_counter()

    consecutive_empty = 0
    last_account_create_ledger = 0

    # Fetch fee once, then rely on WS ledgerClosed updates (or re-fetch on telINSUF_FEE_P)
    if wl._cached_fee is None:
        try:
            fee_info = await asyncio.wait_for(wl.get_fee_info(), timeout=5.0)
            wl._cached_fee = fee_info.minimum_fee
            log.info("workload: fetched base fee = %d drops", wl._cached_fee)
        except Exception as e:
            log.warning("workload: initial fee fetch failed: %s", e)

    try:
        while not workload_stop_event.is_set():
            try:
                # Wait for first ledger index from WS
                batch_ledger = wl.latest_ledger_index
                if batch_ledger == 0:
                    await asyncio.sleep(0.5)
                    continue
                batch_lls = batch_ledger + C.HORIZON

                # Re-fetch fee if invalidated (telINSUF_FEE_P sets _cached_fee = None)
                if wl._cached_fee is None:
                    try:
                        fee_info = await asyncio.wait_for(wl.get_fee_info(), timeout=2.0)
                        wl._cached_fee = fee_info.minimum_fee
                        log.info("workload: refreshed fee = %d drops (fee escalation detected)", wl._cached_fee)
                    except Exception:
                        pass
                batch_fee = wl._cached_fee or 10

                # Find free accounts
                pending_counts = wl.get_pending_txn_counts_by_account()
                free_accounts = [addr for addr in wl.wallets if pending_counts.get(addr, 0) == 0]

                n_free = len(free_accounts)
                if n_free == 0:
                    consecutive_empty += 1
                    if consecutive_empty == 1 or consecutive_empty % 5 == 0:
                        log.warning(
                            "workload: 0 free accounts (consecutive=%d, total=%d, pending=%d)",
                            consecutive_empty,
                            len(wl.wallets),
                            sum(pending_counts.values()),
                        )
                    # Self-healing: force-expire txns past their LastLedgerSequence
                    if consecutive_empty >= 3:
                        expired = wl.expire_past_lls(batch_ledger)
                        if expired:
                            log.warning("workload: self-heal expired %d stale txns at ledger %d", expired, batch_ledger)
                            pending_counts = wl.get_pending_txn_counts_by_account()
                            free_accounts = [addr for addr in wl.wallets if pending_counts.get(addr, 0) == 0]
                            n_free = len(free_accounts)
                            if n_free > 0:
                                log.info("workload: self-heal freed %d accounts", n_free)
                        elif consecutive_empty % 10 == 0:
                            diag = wl.diagnostics_snapshot()
                            log.warning(
                                "workload: STUCK for %d iterations — blocked=%d free=%d pending_by_state=%s oldest_age=%d",
                                consecutive_empty,
                                diag["blocked_accounts"],
                                diag["free_accounts"],
                                diag["pending_by_state"],
                                diag["oldest_pending_age_ledgers"],
                            )
                    if n_free == 0:
                        await asyncio.sleep(0.5)
                        continue
                else:
                    if consecutive_empty >= 3:
                        log.info(
                            "workload: recovered — %d free accounts after %d empty iterations", n_free, consecutive_empty
                        )
                    consecutive_empty = 0

                # Occasionally grow the account pool (once per ledger)
                current_ledger = wl.latest_ledger_index
                if (
                    current_ledger > last_account_create_ledger
                    and random() < 0.50
                    and "Payment" not in wl.disabled_txn_types
                ):
                    funding_pending = pending_counts.get(wl.funding_wallet.address, 0)
                    if funding_pending == 0:
                        try:
                            default_balance = wl.config["users"]["default_balance"]
                            large_balance = str(int(default_balance) * 10)
                            await wl.create_account(initial_xrp_drops=large_balance)
                            workload_stats["submitted"] += 1
                            last_account_create_ledger = current_ledger
                        except Exception as e:
                            log.debug("Failed to create new account: %s", e)
                            workload_stats["failed"] += 1

                # Build + sign txns for free accounts
                # TODO: Parallelize this loop — alloc_seq RPCs and signing are sequential.
                # With 400 accounts, build phase takes 2-3s. Use TaskGroup for alloc_seq,
                # then sign concurrently. This is the main throughput bottleneck.
                target = wl.target_txns_per_ledger
                build_start = perf_counter()
                batch: list[PendingTx] = []
                for addr in free_accounts[:target]:
                    wallet = wl.wallets[addr]
                    txn_type = pick_eligible_txn_type(wallet, wl.ctx)
                    if txn_type is None:
                        continue

                    try:
                        ctx = replace(wl.ctx, forced_account=wallet)
                        composed = build_txn_dict(txn_type, ctx)
                        if composed is None:
                            continue
                        txn = txn_model_cls(txn_type).from_xrpl(composed)
                    except Exception as e:
                        log.debug("build %s/%s: %s", addr, txn_type, e)
                        continue

                    try:
                        seq = await wl.alloc_seq(wallet.address)
                    except Exception as e:
                        log.debug("alloc_seq %s: %s", addr, e)
                        continue

                    try:
                        pending = await wl.build_sign_and_track(
                            txn,
                            wallet,
                            fee_drops=batch_fee,
                            created_ledger=batch_ledger,
                            last_ledger_seq=batch_lls,
                            preallocated_seq=seq,
                        )
                        batch.append(pending)
                    except Exception as e:
                        await wl.release_seq(wallet.address, seq)
                        log.debug("sign %s/%s: %s", addr, txn_type, e)
                        continue

                if not batch:
                    await asyncio.sleep(0)
                    continue

                build_ms = (perf_counter() - build_start) * 1000
                log.info(
                    "Batch: %d txns, ledger=%d, wallets=%d, build=%.0fms",
                    len(batch), batch_ledger, len(wl.wallets), build_ms,
                )

                # Submit all in parallel — no waiting, fire immediately
                try:
                    async with asyncio.TaskGroup() as tg:
                        tasks = [tg.create_task(wl.submit_pending(p)) for p in batch]

                    submitted = 0
                    failed = 0
                    errors: dict[str, int] = {}
                    for task in tasks:
                        result = task.result()
                        if result is None:
                            failed += 1
                            errors["build_failed"] = errors.get("build_failed", 0) + 1
                        else:
                            er = result.get("engine_result")
                            if er and er not in ("terQUEUED",) and er.startswith(("ter", "tem", "tef", "tel")):
                                failed += 1
                                errors[er] = errors.get(er, 0) + 1
                            else:
                                submitted += 1
                    workload_stats["submitted"] += submitted
                    workload_stats["failed"] += failed
                    if "telINSUF_FEE_P" in errors:
                        wl._cached_fee = None
                        log.warning("Fee escalation detected — invalidating cached fee")
                    if failed:
                        log.info("Batch result: %d ok, %d failed — errors=%s", submitted, failed, errors)
                    else:
                        log.info("Batch result: %d ok", submitted)
                except* Exception as eg:
                    for exc in eg.exceptions:
                        log.error("Batch error: %s: %s", type(exc).__name__, exc)
                    workload_stats["failed"] += len(batch)

            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.error("WORKLOAD CRASH (recovering): %s: %s", type(e).__name__, e, exc_info=True)
                await asyncio.sleep(1)

    except asyncio.CancelledError:
        log.debug("Continuous workload cancelled")
        raise
    finally:
        log.debug("Continuous workload stopped — stats: %s", workload_stats)


@r_workload.post("/start")
async def start_workload():
    """Start continuous random transaction workload."""
    global workload_running, workload_stop_event, workload_task, workload_stats

    if workload_running:
        raise HTTPException(status_code=400, detail="Workload already running")

    workload_stats = {"submitted": 0, "validated": 0, "failed": 0, "started_at": perf_counter()}

    log.info("Starting workload")
    workload_stop_event = asyncio.Event()
    workload_task = asyncio.create_task(continuous_workload(), name="continuous_workload")
    workload_task.add_done_callback(_log_task_exception)
    workload_running = True
    app.state.workload.workload_started = True

    return {
        "status": "started",
        "message": "Continuous workload started - submitting random transactions at expected_ledger_size + 1 per ledger (max 200)",
    }


@r_workload.post("/stop")
async def stop_workload():
    """Stop continuous workload."""
    global workload_running, workload_stop_event, workload_task

    if not workload_running:
        raise HTTPException(status_code=400, detail="Workload not running")

    log.info("Stopping workload")
    workload_stop_event.set()
    await workload_task
    stop_ledger = await app.state.workload._current_ledger_index()
    log.info("Stopped workload at ledger %s", stop_ledger)
    workload_running = False
    app.state.workload.workload_started = False

    return {"status": "stopped", "stats": workload_stats}


@r_workload.get("/status")
async def workload_status():
    """Get current workload status and statistics."""
    wl = app.state.workload
    return {
        "running": workload_running,
        "stats": workload_stats,
        "uptime_seconds": round(time.time() - wl.started_at),
        "started_at": wl.started_at,
    }


class TargetTxnsReq(BaseModel):
    target_txns: int


@r_workload.get("/target-txns")
async def get_target_txns():
    """Get current target transactions per ledger for continuous workload.

    Returns the hard cap on transactions submitted per ledger.
    """
    return {
        "target_txns_per_ledger": app.state.workload.target_txns_per_ledger,
        "description": "Hard cap on transactions per ledger",
    }


@r_workload.post("/target-txns")
async def set_target_txns(req: TargetTxnsReq):
    """Set target transactions per ledger for continuous workload.

    Controls how many transactions to submit per ledger.
    - Lower (10-20): Very conservative, smooth, low throughput
    - Medium (30-50): Balanced approach
    - Higher (80-100): Aggressive, high throughput

    Takes effect immediately on next batch.
    """
    if req.target_txns < 0 or req.target_txns > 1000:
        raise HTTPException(status_code=400, detail="target_txns must be between 0 and 1000")

    old_value = app.state.workload.target_txns_per_ledger
    app.state.workload.target_txns_per_ledger = req.target_txns

    log.info(f"target_txns_per_ledger changed: {old_value} -> {app.state.workload.target_txns_per_ledger}")

    return {
        "old_value": old_value,
        "new_value": app.state.workload.target_txns_per_ledger,
        "status": "updated",
        "note": "Change takes effect on next workload batch",
    }


@r_workload.get("/disabled-types")
async def get_disabled_types():
    """Get currently disabled transaction types for random generation."""
    from workload.txn_factory.builder import _BUILDERS

    all_types = list(_BUILDERS.keys())
    disabled = sorted(app.state.workload.disabled_txn_types)
    enabled = [t for t in all_types if t not in app.state.workload.disabled_txn_types]
    return {
        "all_types": all_types,
        "enabled_types": enabled,
        "disabled_types": disabled,
        "config_disabled": sorted(app.state.workload._config_disabled_types),
    }


class ToggleTypeReq(BaseModel):
    txn_type: str
    enabled: bool


@r_workload.post("/toggle-type")
async def toggle_txn_type(req: ToggleTypeReq):
    """Toggle a single transaction type on or off for random generation.

    Takes effect immediately on the next transaction generation.
    """
    from workload.txn_factory.builder import _BUILDERS

    if req.txn_type not in _BUILDERS:
        raise HTTPException(
            status_code=400, detail=f"Unknown transaction type: {req.txn_type}. Valid: {list(_BUILDERS.keys())}"
        )

    if req.txn_type in app.state.workload._config_disabled_types:
        raise HTTPException(
            status_code=400,
            detail=f"{req.txn_type} is disabled in config.toml (amendment not available). Cannot toggle at runtime.",
        )

    if req.enabled:
        app.state.workload.disabled_txn_types.discard(req.txn_type)
    else:
        app.state.workload.disabled_txn_types.add(req.txn_type)

    return {
        "txn_type": req.txn_type,
        "enabled": req.enabled,
        "disabled_types": sorted(app.state.workload.disabled_txn_types),
    }


r_network = APIRouter(prefix="/network", tags=["Network"])


@r_network.post("/reset")
async def network_reset():
    """Reset the network: stop workload, regenerate testnet, restart containers, restart workload.

    This calls `gen auto` to regenerate the testnet, then `docker compose down/up`.
    Requires gen CLI and docker compose to be available on the host.
    """
    import shutil
    import subprocess

    # 1. Stop workload if running
    global workload_running, workload_stop_event, workload_task
    if workload_running and workload_stop_event:
        workload_stop_event.set()
        if workload_task:
            try:
                await workload_task
            except Exception:
                pass
        workload_running = False

    testnet_dir = os.getenv("TESTNET_DIR", str(Path(__file__).resolve().parents[3] / "prepare-workload" / "testnet"))
    gen_bin = os.getenv("GEN_BIN", "gen")

    steps = []

    # 2. Docker compose down
    try:
        r = subprocess.run(
            ["docker", "compose", "down"],
            cwd=testnet_dir,
            capture_output=True,
            text=True,
            timeout=30,
        )
        steps.append({"step": "docker compose down", "returncode": r.returncode, "stderr": r.stderr.strip()})
    except Exception as e:
        steps.append({"step": "docker compose down", "error": str(e)})

    # 3. Clean old artifacts
    for name in ["volumes", "ledger.json", "accounts.json", "docker-compose.yml"]:
        target = Path(testnet_dir) / name
        if target.is_dir():
            shutil.rmtree(target)
        elif target.is_file():
            target.unlink()
    # Clean state.db (check both workload cwd and parent)
    for db_path in [Path("state.db"), Path(__file__).resolve().parents[3] / "state.db"]:
        if db_path.is_file():
            db_path.unlink()
    steps.append({"step": "clean artifacts", "status": "ok"})

    # 4. Regenerate with library API
    try:
        from workload.gen_cmd import run_gen

        num_validators = int(os.getenv("TESTNET_VALIDATORS", "5"))
        run_gen(output_dir=testnet_dir, num_validators=num_validators)
        steps.append({"step": "gen (library)", "status": "ok"})
    except Exception as e:
        steps.append({"step": "gen (library)", "error": str(e)})

    # 5. Docker compose up
    try:
        r = subprocess.run(
            ["docker", "compose", "up", "-d"],
            cwd=testnet_dir,
            capture_output=True,
            text=True,
            timeout=60,
        )
        steps.append({"step": "docker compose up", "returncode": r.returncode, "stderr": r.stderr.strip()[-200:]})
    except Exception as e:
        steps.append({"step": "docker compose up", "error": str(e)})

    return {
        "status": "reset complete — restart the workload process to reconnect",
        "steps": steps,
    }


@r_transaction.post("/ammdeposit")
async def create_ammdeposit():
    """Create and submit an AMMDeposit transaction."""
    return await create("AMMDeposit")


@r_transaction.post("/ammwithdraw")
async def create_ammwithdraw():
    """Create and submit an AMMWithdraw transaction."""
    return await create("AMMWithdraw")


@r_transaction.post("/delegateset")
async def create_delegateset():
    """Create and submit a DelegateSet transaction."""
    return await create("DelegateSet")


@r_transaction.post("/credentialcreate")
async def create_credentialcreate():
    """Create and submit a CredentialCreate transaction."""
    return await create("CredentialCreate")


@r_transaction.post("/credentialaccept")
async def create_credentialaccept():
    """Create and submit a CredentialAccept transaction."""
    return await create("CredentialAccept")


@r_transaction.post("/credentialdelete")
async def create_credentialdelete():
    """Create and submit a CredentialDelete transaction."""
    return await create("CredentialDelete")


@r_transaction.post("/permissioneddomainset")
async def create_permissioneddomainset():
    """Create and submit a PermissionedDomainSet transaction."""
    return await create("PermissionedDomainSet")


@r_transaction.post("/permissioneddomaindelete")
async def create_permissioneddomaindelete():
    """Create and submit a PermissionedDomainDelete transaction."""
    return await create("PermissionedDomainDelete")


@r_transaction.post("/vaultcreate")
async def create_vaultcreate():
    """Create and submit a VaultCreate transaction."""
    return await create("VaultCreate")


@r_transaction.post("/vaultset")
async def create_vaultset():
    """Create and submit a VaultSet transaction."""
    return await create("VaultSet")


@r_transaction.post("/vaultdelete")
async def create_vaultdelete():
    """Create and submit a VaultDelete transaction."""
    return await create("VaultDelete")


@r_transaction.post("/vaultdeposit")
async def create_vaultdeposit():
    """Create and submit a VaultDeposit transaction."""
    return await create("VaultDeposit")


@r_transaction.post("/vaultwithdraw")
async def create_vaultwithdraw():
    """Create and submit a VaultWithdraw transaction."""
    return await create("VaultWithdraw")


@r_transaction.post("/vaultclawback")
async def create_vaultclawback():
    """Create and submit a VaultClawback transaction."""
    return await create("VaultClawback")


@r_dex.get("/metrics")
async def dex_metrics():
    """Get DEX metrics including AMM pool states, trading activity counts."""
    return app.state.workload.snapshot_dex_metrics()


@r_dex.get("/pools")
async def dex_pools():
    """List all tracked AMM pools."""
    w: Workload = app.state.workload
    return {
        "total_pools": len(w.amm.pools),
        "pools": w.amm.pools,
    }


@r_dex.get("/pools/{index}")
async def dex_pool_detail(index: int):
    """Get detailed amm_info for a specific pool by index."""
    from xrpl.models.currencies import XRP as XRPCurrency
    from xrpl.models.requests import AMMInfo

    w: Workload = app.state.workload
    if index >= len(w.amm.pools):
        raise HTTPException(status_code=404, detail=f"Pool index {index} not found")

    pool = w.amm.pools[index]
    asset1, asset2 = pool["asset1"], pool["asset2"]

    if asset1.get("currency") == "XRP":
        a1 = XRPCurrency()
    else:
        from xrpl.models import IssuedCurrency

        a1 = IssuedCurrency(currency=asset1["currency"], issuer=asset1["issuer"])

    if asset2.get("currency") == "XRP":
        a2 = XRPCurrency()
    else:
        from xrpl.models import IssuedCurrency

        a2 = IssuedCurrency(currency=asset2["currency"], issuer=asset2["issuer"])

    resp = await w._rpc(AMMInfo(asset=a1, asset2=a2))
    return resp.result


@r_dex.post("/poll")
async def dex_poll_now():
    """Manually trigger a DEX metrics poll."""
    return await app.state.workload.poll_dex_metrics()


app.include_router(r_accounts)
app.include_router(r_pay)
app.include_router(r_pay, prefix="/pay", include_in_schema=False)  # alias /pay/ for convenience
app.include_router(r_transaction, prefix="/transaction")
app.include_router(r_transaction, prefix="/txn", include_in_schema=False)  # alias /txn/ because I'm sick of typing...
app.include_router(r_state)
app.include_router(r_workload)
app.include_router(r_dex)
app.include_router(r_network)


# ─────────────────────────────────────────────────────────────────────────────
# Logs
# ─────────────────────────────────────────────────────────────────────────────

_LOG_FILE = Path("/tmp/workload.log")
_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}


@app.get("/logs", tags=["Logs"])
async def get_logs(n: int = 200, level: str | None = None):
    """Return the last n log lines, optionally filtered to a minimum level."""
    if not _LOG_FILE.exists():
        return {"lines": [], "file": str(_LOG_FILE)}

    level = level.upper() if level else None
    if level and level not in _LOG_LEVELS:
        raise HTTPException(status_code=400, detail=f"level must be one of {_LOG_LEVELS}")

    lines = _LOG_FILE.read_text(errors="replace").splitlines()

    if level:
        priority = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
        min_idx = priority.index(level)
        lines = [l for l in lines if any(f" {lvl} " in l or f" {lvl:<8}" in l for lvl in priority[min_idx:])]

    return {"lines": lines[-n:], "total_matching": len(lines), "file": str(_LOG_FILE)}


@app.get("/logs/page", response_class=HTMLResponse, tags=["Logs"])
async def logs_page(n: int = 300, level: str = "WARNING"):
    """Live log viewer — auto-refreshes every 3 seconds."""
    level_opts = "".join(
        f'<option value="{lv}"{" selected" if lv == level else ""}>{lv}</option>'
        for lv in ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>workload logs</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font: 13px/1.5 "Fira Mono", "Cascadia Code", monospace; background: #0d1117; color: #c9d1d9; }}
  header {{ display: flex; align-items: center; gap: 1rem; padding: 0.6rem 1rem;
            background: #161b22; border-bottom: 1px solid #30363d; }}
  header h1 {{ font-size: 0.95rem; color: #58a6ff; }}
  select, input {{ background: #0d1117; color: #c9d1d9; border: 1px solid #30363d;
                   border-radius: 4px; padding: 0.2rem 0.4rem; font: inherit; }}
  label {{ font-size: 0.8rem; color: #8b949e; }}
  #log {{ padding: 0.8rem 1rem; white-space: pre-wrap; word-break: break-all; }}
  .WARNING {{ color: #d29922; }}
  .ERROR   {{ color: #f85149; }}
  .CRITICAL {{ color: #ff7b72; font-weight: bold; }}
  .DEBUG   {{ color: #6e7681; }}
  .INFO    {{ color: #c9d1d9; }}
  #status  {{ margin-left: auto; font-size: 0.75rem; color: #6e7681; }}
</style>
</head>
<body>
<header>
  <h1>workload logs</h1>
  <label>Min level
    <select id="lvl" onchange="reload()">{level_opts}</select>
  </label>
  <label>Lines
    <input type="number" id="nlines" value="{n}" min="10" max="2000" style="width:5rem" onchange="reload()">
  </label>
  <span id="status">loading...</span>
</header>
<div id="log"></div>
<script>
  function levelClass(line) {{
    for (const lv of ['CRITICAL','ERROR','WARNING','INFO','DEBUG'])
      if (line.includes(' ' + lv + ' ') || line.includes(lv.padEnd(8))) return lv;
    return '';
  }}
  async function reload() {{
    const lv = document.getElementById('lvl').value;
    const n  = document.getElementById('nlines').value;
    try {{
      const r = await fetch(`/logs?n=${{n}}&level=${{lv}}`);
      const d = await r.json();
      const html = d.lines.map(l => {{
        const cls = levelClass(l);
        const esc = l.replace(/&/g,'&amp;').replace(/</g,'&lt;');
        return cls ? `<span class="${{cls}}">${{esc}}</span>` : esc;
      }}).join('\\n');
      document.getElementById('log').innerHTML = html;
      document.getElementById('status').textContent =
        `${{d.lines.length}} lines shown / ${{d.total_matching}} matching — ${{new Date().toLocaleTimeString()}}`;
      window.scrollTo(0, document.body.scrollHeight);
    }} catch(e) {{ document.getElementById('status').textContent = 'fetch error: ' + e; }}
  }}
  reload();
  setInterval(reload, 3000);
</script>
</body>
</html>"""

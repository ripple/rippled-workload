import asyncio
import contextlib
import logging
import os
from contextlib import asynccontextmanager
from time import perf_counter

import httpx
from fastapi import APIRouter, FastAPI
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, PositiveInt
from xrpl.asyncio.clients import AsyncJsonRpcClient, AsyncWebsocketClient
from xrpl.models import StreamParameter, Subscribe
from xrpl.models.transactions import Payment

from workload.ws import ws_listener
from workload.ws_processor import process_ws_events

try:
    from antithesis.lifecycle import setup_complete

    ANTITHESIS_AVAILABLE = True
except ImportError:
    ANTITHESIS_AVAILABLE = False

    def setup_complete(details=None):
        pass


import asyncio
import contextlib
import json
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import xrpl
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import AnyUrl, BaseModel
from xrpl.asyncio.clients import AsyncWebsocketClient
from xrpl.models import StreamParameter, Subscribe

import workload.constants as C
from workload.config import cfg
from workload.logging_config import setup_logging
from workload.txn_factory.builder import generate_txn
from workload.workload_core import ValidationRecord, Workload, periodic_finality_check

setup_logging()
log = logging.getLogger("workload.app")

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
WS = "ws://rippled:6006"


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
                log.error(f"RPC failed after {max_retries} attempts")
                raise


async def wait_for_ledgers(url: str, count: int) -> None:
    """
    Connects to the rippled WebSocket and waits for 'count' ledgers to close.
    """
    log.info(f"Connecting to WebSocket {url} to wait for {count} ledgers...")
    try:
        async with AsyncWebsocketClient(url) as client:
            await client.send(Subscribe(streams=[StreamParameter.LEDGER]))
            ledger_count = 0
            async for msg in client:
                if msg.get("type") == "ledgerClosed":
                    ledger_count += 1
                    log.info("Ledger %s closed. (%s/%s)", msg.get("ledger_index"), ledger_count, count)
                    if ledger_count >= count:
                        log.info("Observed %s ledgers closed. Convinced network is progessing.", ledger_count)
                        break
    except Exception as e:
        log.error(f"Failed to wait for ledgers via WebSocket: {e}")
        raise  # Fail startup if we can't confirm network status


async def _dump_tasks(tag: str):
    log.debug("=== TASK DUMP: %s ===", tag)
    for t in asyncio.all_tasks():
        if t is asyncio.current_task():
            continue
        log.debug("task %r done=%s cancelled=%s", t.get_name(), t.done(), t.cancelled())
        for frame in t.get_stack(limit=5):
            log.debug("  at %s:%s in %s", frame.f_code.co_filename, frame.f_lineno, frame.f_code.co_name)


@asynccontextmanager
async def lifespan(app: FastAPI):
    check_interval = 2
    stop = asyncio.Event()

    async with asyncio.timeout(OVERALL_STARTUP_TIMEOUT):
        log.info("Probing RPC endpoint...")
        await _probe_rippled(RPC)
        log.info("RPC OK. Waiting for network to be ready (seeing ledger progress)")
        await wait_for_ledgers(WS, LEDGERS_TO_WAIT)

    log.info("Network is ready. Initializing workload...")

    from workload.sqlite_store import SQLiteStore

    client = AsyncJsonRpcClient(RPC)
    sqlite_store = SQLiteStore(db_path="state.db")
    app.state.workload = Workload(cfg, client, store=sqlite_store)
    app.state.stop = stop

    app.state.ws_queue = asyncio.Queue(maxsize=1000)  # TODO: Constant
    log.debug("Created WS event queue (maxsize=1000)")

    app.state.ws_stop_event = asyncio.Event()
    async with asyncio.TaskGroup() as tg:
        app.state.tg = tg

        tg.create_task(
            ws_listener(
                app.state.stop, WS, app.state.ws_queue, accounts_provider=app.state.workload.get_all_account_addresses
            ),
            name="ws_listener",
        )

        tg.create_task(
            periodic_finality_check(app.state.workload, app.state.stop, check_interval), name="finality_checker"
        )

        log.info("Background tasks started:")
        log.info("ws_processor")
        log.info("RPC finality_checker")

        state_loaded = app.state.workload.load_state_from_store()

        if state_loaded:
            log.debug("Loaded existing state from database, skipping network provisioning")
            log.debug(
                "  Wallets: %s (Gateways: %s, Users: %s)",
                len(app.state.workload.wallets),
                len(app.state.workload.gateways),
                len(app.state.workload.users),
            )
        else:
            gw, u = cfg["gateways"], cfg["users"]
            log.info("No persisted state found. Initializing participants (gateways=%s, users=%s)...", gw, u)
            init_result = await app.state.workload.init_participants(gateway_cfg=gw, user_cfg=u)
            app.state.workload.update_txn_context()
            log.info(
                "Accounts initialized: %s gateways, %s users.", len(init_result["gateways"]), len(init_result["users"])
            )

        init_ledger = await app.state.workload._current_ledger_index()
        workload_ready_msg = "Workload initialization complete"
        log.info(f"Network initialization complete at ledger {init_ledger}. Ready to accept requests!")
        setup_complete(
            {
                "message": workload_ready_msg,
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
        await asyncio.sleep(5)
        await start_workload()
        try:
            yield
        finally:
            log.info("Shutting down...")
            stop.set()
            app.state.ws_stop_event.set()

            await asyncio.sleep(5)
            log.info("Exiting TaskGroup (will cancel any remaining tasks)...")

    log.info("Shutdown complete")


app = FastAPI(
    title="XRPL Workload",
    debug=True,
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


class PaymentReq(BaseModel):
    sender_address: str = cfg["funding_account"]["address"]
    receiver_address: str = xrpl.wallet.Wallet.create().address
    drops: PositiveInt = 10


class SendPaymentReq(BaseModel):
    source: str
    destination: str
    amount: str | dict  # XRP drops as string, or IOU as {"currency": "USD", "issuer": "r...", "value": "100"}


@app.get("/health")
def health():
    return {"status": "ok"}  # Not the most thorough of healthchecks...


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
    if isinstance(w.store, SQLiteStore):
        balances = w.store.get_balances(account_id)
        return {"account": account_id, "balances": balances}
    else:
        raise HTTPException(status_code=503, detail="Balance tracking not available (not using SQLiteStore)")


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
    log.debug("response frmo submit_pending() %s", res)
    print("Submit result:", res)
    return res


@r_state.get("/summary")
async def state_summary():
    return app.state.workload.snapshot_stats()


@r_state.get("/dashboard", response_class=HTMLResponse)
async def state_dashboard():
    """HTML dashboard with live stats and visual progress bars."""
    stats = app.state.workload.snapshot_stats()
    failed_data = app.state.workload.snapshot_failed()

    wl = app.state.workload
    fee_info = await wl.get_fee_info()
    hostname = RPC.split("//")[1].split(":")[0] if "//" in RPC else RPC.split(":")[0]

    total = stats.get("total_tracked", 0)
    by_state = stats.get("by_state", {})

    validated = by_state.get("VALIDATED", 0)
    rejected = by_state.get("REJECTED", 0)
    submitted = by_state.get("SUBMITTED", 0)
    created = by_state.get("CREATED", 0)
    retryable = by_state.get("RETRYABLE", 0)
    expired = by_state.get("EXPIRED", 0)

    val_pct = (validated / total * 100) if total > 0 else 0
    rej_pct = (rejected / total * 100) if total > 0 else 0

    INTERNAL_STATES = {"CASCADE_EXPIRED", "unknown", None, ""}
    failures_by_result = {}
    for failed in failed_data:
        result = failed.get("engine_result_first", "unknown")
        if result not in INTERNAL_STATES and not (result and result.startswith("tes")):
            failures_by_result[result] = failures_by_result.get(result, 0) + 1

    top_failures = sorted(failures_by_result.items(), key=lambda x: x[1], reverse=True)[:10]

    submission_results = stats.get("submission_results", {})
    sorted_submission_results = sorted(submission_results.items(), key=lambda x: x[1], reverse=True)

    html_content = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Workload Dashboard</title>
        <meta http-equiv="refresh" content="1">
        <style>
            body {{
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                background: #0d1117;
                color: #c9d1d9;
                margin: 0;
                padding: 20px;
            }}
            .container {{
                max-width: 1200px;
                margin: 0 auto;
            }}
            h1 {{
                color: #58a6ff;
                margin-bottom: 10px;
            }}
            .subtitle {{
                color: #8b949e;
                margin-bottom: 30px;
            }}
            .stats-grid {{
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
                gap: 20px;
                margin-bottom: 30px;
            }}
            .stat-card {{
                background: #161b22;
                border: 1px solid #30363d;
                border-radius: 6px;
                padding: 20px;
            }}
            .stat-label {{
                color: #8b949e;
                font-size: 12px;
                text-transform: uppercase;
                margin-bottom: 8px;
            }}
            .stat-value {{
                font-size: 32px;
                font-weight: bold;
                margin-bottom: 4px;
            }}
            .stat-value.success {{ color: #3fb950; }}
            .stat-value.error {{ color: #f85149; }}
            .stat-value.warning {{ color: #d29922; }}
            .stat-value.info {{ color: #58a6ff; }}
            .stat-percentage {{
                color: #8b949e;
                font-size: 14px;
            }}
            .progress-bar {{
                background: #21262d;
                border-radius: 6px;
                height: 8px;
                overflow: hidden;
                margin-top: 8px;
            }}
            .progress-fill {{
                height: 100%;
                transition: width 0.3s ease;
            }}
            .progress-fill.success {{ background: #3fb950; }}
            .progress-fill.error {{ background: #f85149; }}
            .failures-table {{
                background: #161b22;
                border: 1px solid #30363d;
                border-radius: 6px;
                padding: 20px;
            }}
            table {{
                width: 100%;
                border-collapse: collapse;
            }}
            th, td {{
                text-align: left;
                padding: 12px;
                border-bottom: 1px solid #21262d;
            }}
            th {{
                color: #8b949e;
                font-weight: 600;
                font-size: 12px;
                text-transform: uppercase;
            }}
            tr:last-child td {{
                border-bottom: none;
            }}
            .badge {{
                display: inline-block;
                padding: 2px 8px;
                border-radius: 4px;
                font-size: 12px;
                font-weight: 600;
            }}
            .badge.error {{ background: #f851491a; color: #f85149; }}
            .controls {{
                margin-bottom: 20px;
                display: flex;
                gap: 10px;
            }}
            .btn {{
                padding: 10px 20px;
                border: none;
                border-radius: 6px;
                font-size: 14px;
                font-weight: 600;
                cursor: pointer;
                transition: opacity 0.2s;
            }}
            .btn:hover {{
                opacity: 0.8;
            }}
            .btn-start {{
                background: #3fb950;
                color: white;
            }}
            .btn-stop {{
                background: #f85149;
                color: white;
            }}
        </style>
    </head>
    <body>
        <div class="container">
            <h1>🚀 Workload Dashboard</h1>
            <div class="subtitle">Live monitoring • Auto-refresh every 1s • Ledger {fee_info.ledger_current_index} @ {hostname}</div>

            <div class="controls">
                <button class="btn btn-start" onclick="fetch('/workload/start', {{method: 'POST'}}).then(() => location.reload())">▶️ Start Workload</button>
                <button class="btn btn-stop" onclick="fetch('/workload/stop', {{method: 'POST'}}).then(() => location.reload())">⏹️ Stop Workload</button>
            </div>

            <div class="stats-grid">
                <div class="stat-card">
                    <div class="stat-label">Fee (min/open/base)</div>
                    <div class="stat-value {"warning" if fee_info.minimum_fee > fee_info.base_fee else "success"}">{fee_info.minimum_fee}/{fee_info.open_ledger_fee}/{fee_info.base_fee}</div>
                    <div class="stat-percentage">drops</div>
                </div>

                <div class="stat-card">
                    <div class="stat-label">Queue Utilization</div>
                    <div class="stat-value info">{fee_info.current_queue_size}/{fee_info.max_queue_size}</div>
                    <div class="stat-percentage">{(fee_info.current_queue_size / fee_info.max_queue_size * 100) if fee_info.max_queue_size > 0 else 0:.1f}%</div>
                    <div class="progress-bar">
                        <div class="progress-fill info" style="width: {(fee_info.current_queue_size / fee_info.max_queue_size * 100) if fee_info.max_queue_size > 0 else 0}%"></div>
                    </div>
                </div>

                <div class="stat-card">
                    <div class="stat-label">Ledger Utilization</div>
                    <div class="stat-value info">{fee_info.current_ledger_size}/{fee_info.expected_ledger_size}</div>
                    <div class="stat-percentage">{(fee_info.current_ledger_size / fee_info.expected_ledger_size * 100) if fee_info.expected_ledger_size > 0 else 0:.1f}%</div>
                    <div class="progress-bar">
                        <div class="progress-fill info" style="width: {(fee_info.current_ledger_size / fee_info.expected_ledger_size * 100) if fee_info.expected_ledger_size > 0 else 0}%"></div>
                    </div>
                </div>
            </div>

            <div class="stats-grid">
                <div class="stat-card">
                    <div class="stat-label">Total Transactions</div>
                    <div class="stat-value info">{total:,}</div>
                </div>

                <div class="stat-card">
                    <div class="stat-label">Validated</div>
                    <div class="stat-value success">{validated:,}</div>
                    <div class="stat-percentage">{val_pct:.1f}%</div>
                    <div class="progress-bar">
                        <div class="progress-fill success" style="width: {val_pct}%"></div>
                    </div>
                </div>

                <div class="stat-card">
                    <div class="stat-label">Rejected</div>
                    <div class="stat-value error">{rejected:,}</div>
                    <div class="stat-percentage">{rej_pct:.1f}%</div>
                    <div class="progress-bar">
                        <div class="progress-fill error" style="width: {rej_pct}%"></div>
                    </div>
                </div>

                <div class="stat-card">
                    <div class="stat-label">In-Flight</div>
                    <div class="stat-value warning">{submitted + created:,}</div>
                    <div class="stat-percentage">Submitted: {submitted} | Created: {created}</div>
                </div>

                <div class="stat-card">
                    <div class="stat-label">Retryable</div>
                    <div class="stat-value warning">{retryable:,}</div>
                    <div class="stat-percentage">terPRE_SEQ waiting</div>
                </div>

                <div class="stat-card">
                    <div class="stat-label">Expired</div>
                    <div class="stat-value">{expired:,}</div>
                </div>
            </div>

            {"<div class='failures-table'><h2>Submission Results</h2><table><thead><tr><th>Engine Result</th><th>Count</th></tr></thead><tbody>" + "".join(f"<tr><td><span class='badge {'success' if result == 'tesSUCCESS' else 'warning' if result and result.startswith('ter') else 'error' if result and result.startswith(('tel', 'tec', 'tem', 'tef')) else 'info'}'>{result}</span></td><td>{count:,}</td></tr>" for result, count in sorted_submission_results) + "</tbody></table></div>" if sorted_submission_results else ""}

            {"<div class='failures-table'><h2>Top Failures</h2><table><thead><tr><th>Error Code</th><th>Count</th></tr></thead><tbody>" + "".join(f"<tr><td><span class='badge error'>{result}</span></td><td>{count:,}</td></tr>" for result, count in top_failures) + "</tbody></table></div>" if top_failures else ""}
        </div>
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


@r_state.get("/expired")
async def state_expired():
    """Get transactions that expired without validating."""
    wl = app.state.workload
    expired = [r for r in wl.snapshot_pending(open_only=False) if r["state"] == "EXPIRED"]
    return {"expired": expired}


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


@r_state.get("/heartbeat")
async def state_heartbeat():
    """
    Return heartbeat status - our canary for ledger health.

    We should see exactly ONE heartbeat transaction per ledger.
    Missing heartbeats indicate network issues, WS disconnection, or other problems.

    Returns:
        - last_heartbeat_ledger: Most recent ledger where heartbeat was attempted
        - total_heartbeats: Total number of heartbeats submitted
        - missed_heartbeats: Ledger indices where heartbeat failed
        - missed_count: Total number of missed heartbeats
        - recent_heartbeats: Last 20 heartbeat attempts with status
    """
    return app.state.workload.snapshot_heartbeat()


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
    }


workload_running = False
workload_stop_event = None
workload_task = None
workload_stats = {"submitted": 0, "validated": 0, "failed": 0, "started_at": None}


async def continuous_workload():
    """Continuously submit XRP payments, respecting 1 pending txn per account.

    Key constraint: Only ONE transaction per account can be in-flight at a time.
    This prevents sequence number conflicts entirely - no resyncs needed.

    We can still submit many transactions in PARALLEL as long as each is from
    a different account.

    Uses XRP-only payments for simplicity and predictable base fees.
    """
    from random import random, sample

    from xrpl.models.transactions import Payment

    global workload_stats
    wl = app.state.workload

    log.debug("🚀 Continuous workload started (XRP payments only)")
    workload_stats["started_at"] = perf_counter()

    try:
        while not workload_stop_event.is_set():
            ledger_size = await wl._expected_ledger_size()

            if random() < 0.50:
                funding_pending = wl.get_pending_txn_counts_by_account().get(wl.funding_wallet.address, 0)
                if funding_pending == 0:
                    try:
                        default_balance = wl.config["users"]["default_balance"]
                        large_balance = str(int(default_balance) * 10)
                        result = await wl.create_account(initial_xrp_drops=large_balance)
                        workload_stats["submitted"] += 1
                        log.debug(f"✓ New account created: {result['address']}...")
                    except Exception as e:
                        log.error(f"Failed to create new account: {e}")
                        workload_stats["failed"] += 1

            pending_counts = wl.get_pending_txn_counts_by_account()

            account_slots = {
                addr: wl.max_pending_per_account - pending_counts.get(addr, 0) for addr in wl.wallets.keys()
            }
            available_accounts = [addr for addr, slots in account_slots.items() if slots > 0]
            total_available_slots = sum(slots for slots in account_slots.values() if slots > 0)

            batch_size = min(total_available_slots, wl.target_txns_per_ledger)

            if batch_size == 0:
                log.debug("No available slots (all accounts at max pending), waiting...")
                await asyncio.sleep(0.5)  # TODO: Remove time
                continue
            current_ledger = await wl._current_ledger_index()
            log.info(
                f"📊 Building batch @ ledger {current_ledger}: {batch_size} txns ({len(available_accounts)} accounts, {total_available_slots} slots, target_size={ledger_size})"
            )

            try:
                from workload.txn_factory.builder import generate_txn

                pending_txns = []
                txns_built = 0
                max_retries = batch_size * 2  # Avoid infinite loop
                retries = 0

                while txns_built < batch_size and retries < max_retries and not workload_stop_event.is_set():
                    retries += 1
                    try:
                        wl.ctx.wallets = list(wl.wallets.values())

                        txn = await generate_txn(wl.ctx)

                        src_addr = txn.account

                        current_pending = wl.get_pending_txn_counts_by_account().get(src_addr, 0)
                        if current_pending >= wl.max_pending_per_account:
                            continue  # Try another transaction

                        pending = await wl.build_sign_and_track(txn, wl.wallets[src_addr])
                        pending_txns.append(pending)
                        txns_built += 1

                    except Exception as e:
                        log.error(f"Failed to build transaction: {e}")
                        workload_stats["failed"] += 1

                if not pending_txns:
                    log.debug("No transactions built this batch")
                    await asyncio.sleep(0.5)  # TODO: Remove time - we tick on LEDGERS not time!
                    continue

                log.info(f"📤 Submitting {len(pending_txns)} transactions in parallel...")

                async with asyncio.TaskGroup() as tg:
                    submit_tasks = [tg.create_task(wl.submit_pending(p)) for p in pending_txns]

                for task in submit_tasks:
                    try:
                        result = task.result()
                        workload_stats["submitted"] += 1
                        er = result.get("engine_result") if result else None
                        if er and er.startswith(("ter", "tem", "tef", "tel")):
                            workload_stats["failed"] += 1
                    except Exception as e:
                        log.error(f"Submit error: {e}")
                        workload_stats["failed"] += 1

            except* Exception as eg:
                for exc in eg.exceptions:
                    log.error(f"Batch error: {type(exc).__name__}: {exc}")
                workload_stats["failed"] += len(pending_txns) if pending_txns else 0

            next_ledger = current_ledger + 1
            while await wl._current_ledger_index() < next_ledger and not workload_stop_event.is_set():
                await asyncio.sleep(0.5)  # TODO: Remove time

    except asyncio.CancelledError:
        log.debug("Continuous workload cancelled")
        raise
    finally:
        log.debug(f"🛑 Continuous workload stopped - Stats: {workload_stats}")


@r_workload.post("/start")
async def start_workload():
    """Start continuous random transaction workload."""
    global workload_running, workload_stop_event, workload_task, workload_stats

    if workload_running:
        raise HTTPException(status_code=400, detail="Workload already running")

    workload_stats = {"submitted": 0, "validated": 0, "failed": 0, "started_at": perf_counter()}

    log.info("Starting workload")
    workload_stop_event = asyncio.Event()
    workload_task = asyncio.create_task(continuous_workload())
    workload_running = True

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

    return {"status": "stopped", "stats": workload_stats}


@r_workload.get("/status")
async def workload_status():
    """Get current workload status and statistics."""
    return {
        "running": workload_running,
        "stats": workload_stats,
        "uptime_seconds": perf_counter() - workload_stats["started_at"] if workload_stats["started_at"] else 0,
    }


@r_workload.get("/fill-fraction")
async def get_fill_fraction():
    """Get current ledger fill fraction for continuous workload.

    Returns the fraction (0.0 to 1.0) of expected_ledger_size used for batch sizing.
    Lower values = smoother distribution across ledgers, higher = more aggressive filling.
    """
    return {
        "fill_fraction": app.state.workload.ledger_fill_fraction,
        "description": "Fraction of ledger_size to fill per batch (0.0 to 1.0)",
        "recommendation": "0.3-0.4 = conservative/smooth, 0.5 = balanced, 0.7-0.8 = aggressive",
    }


class FillFractionReq(BaseModel):
    fill_fraction: float


@r_workload.post("/fill-fraction")
async def set_fill_fraction(req: FillFractionReq):
    """Set ledger fill fraction for continuous workload.

    Controls batch size as fraction of expected_ledger_size.
    - Lower (0.3-0.4): More conservative, smoother distribution, less throughput
    - Medium (0.5): Balanced approach
    - Higher (0.7-0.8): More aggressive, higher throughput, risk of gaps

    Takes effect immediately on next batch.
    """
    if not 0.0 < req.fill_fraction <= 1.0:
        raise HTTPException(status_code=400, detail="fill_fraction must be between 0.0 and 1.0")

    old_value = app.state.workload.ledger_fill_fraction
    app.state.workload.ledger_fill_fraction = req.fill_fraction

    log.info(f"ledger_fill_fraction changed: {old_value} -> {app.state.workload.ledger_fill_fraction}")

    return {
        "old_value": old_value,
        "new_value": app.state.workload.ledger_fill_fraction,
        "status": "updated",
        "note": "Change takes effect on next workload batch",
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
    if req.target_txns < 1 or req.target_txns > 500:
        raise HTTPException(status_code=400, detail="target_txns must be between 1 and 500")

    old_value = app.state.workload.target_txns_per_ledger
    app.state.workload.target_txns_per_ledger = req.target_txns

    log.info(f"target_txns_per_ledger changed: {old_value} -> {app.state.workload.target_txns_per_ledger}")

    return {
        "old_value": old_value,
        "new_value": app.state.workload.target_txns_per_ledger,
        "status": "updated",
        "note": "Change takes effect on next workload batch",
    }


app.include_router(r_accounts)
app.include_router(r_pay)
app.include_router(r_pay, prefix="/pay", include_in_schema=False)  # alias /pay/ for convenience
app.include_router(r_transaction, prefix="/transaction")
app.include_router(r_transaction, prefix="/txn", include_in_schema=False)  # alias /txn/ because I'm sick of typing...
app.include_router(r_state)
app.include_router(r_workload)

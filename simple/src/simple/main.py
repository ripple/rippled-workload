from typing import Final
from datetime import datetime
from dataclasses import dataclass, field
from typing import Any
from fastapi import FastAPI
from antithesis.assertions import always
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel
import asyncio
from contextlib import asynccontextmanager
from xrpl.asyncio.account import get_next_valid_seq_number
from xrpl.asyncio.clients import AsyncWebsocketClient
from xrpl.asyncio.ledger import get_fee
from xrpl.core.addresscodec import classic_address_to_xaddress
from xrpl.core.binarycodec import encode_for_signing, encode
from xrpl.core.keypairs import generate_seed, derive_keypair, derive_classic_address, sign as keypairs_sign
from xrpl.models import SubmitOnly
from xrpl.models import Subscribe, Unsubscribe, StreamParameter
from xrpl.models.transactions import Payment
from xrpl.models.requests import Fee, ServerInfo
from xrpl.transaction import transaction_json_to_binary_codec_form
from datetime import datetime
import logging

from xrpl.asyncio.clients import AsyncWebsocketClient
from xrpl.models import Subscribe, Unsubscribe, StreamParameter

from xrpl.asyncio.clients import AsyncJsonRpcClient


genesis_account = {
    "public_key": "0330E7FC9D56BB25D6893BA3F317AE5BCF33B3291BD63DB32654A313222F7FD020",
    "private_key": "001ACAAEDECE405B2A958212629E16F2EB46B153EEE94CDD350FDEFF52795525B7",
    "address": "rHb9CJAWyB4rj91VRWn96DkukG4bwdtyTh",
    "seed": "snoPBrXtMeMyMHUVTgbuqAfg1SUTb",
}

# ===============================================================
# Typed messages for the unified inbound queue
# ===============================================================

@dataclass(slots=True)
class WSMessage:
    listener: str       # "val0", "val1", "hub0", etc.
    stream: str         # msg["type"]
    payload: dict       # full XRPL WS message

log = logging.getLogger("consensus")

def log_consensus(msg):
    phase = msg.payload.get("consensus")
    ledger = msg.payload.get("ledger_index")
    log.info(f"{msg.listener} phase={phase} ledger={ledger}")

# last = {}
# def log_phase(msg):
#     now = datetime.now()
#     listener = msg.listener
#     phase = msg.payload.get("consensus")

#     if listener in last:
#         diff = (now - last[listener]).total_seconds()
#     else:
#         diff = None

#     print(
#         f"{now.strftime('%H:%M:%S.%f')[:-3]} "
#         f"{listener:<5} phase={phase:<9} "
#         f"Δ={diff:.3f}s" if diff is not None else "───",
#         flush=True
#     )

#     last[listener] = now
# ===============================================================
# Websocket Listener
# ===============================================================

async def ws_listener(
    name: str,
    url: str,
    streams: list[StreamParameter],
    inbound: asyncio.Queue,
    outbound: asyncio.Queue,
    stop: asyncio.Event,
):
    """XRPL WebSocket listener with:
    - auto reconnect
    - nonblocking message handling
    - unified inbound queue
    - unified outbound queue (for sending commands)
    """

    while not stop.is_set():
        print(f"[ws:{name}] connecting to {url}")

        try:
            async with AsyncWebsocketClient(url) as client:
                print(f"[ws:{name}] OPEN")

                # Spawn background reader task
                async def on_message():
                    async for msg in client:
                        stream_type = msg.get("type", "unknown")
                        await inbound.put(
                            WSMessage(listener=name, stream=stream_type, payload=msg)
                        )

                msg_task = asyncio.create_task(on_message())

                # Subscribe to requested streams
                if streams:
                    await client.send(Subscribe(streams=streams))
                    print(f"[ws:{name}] subscribed to {streams}")

                # Main loop: send outgoing messages + check for stop
                while not stop.is_set():
                    try:
                        outgoing = outbound.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                    else:
                        await client.send(outgoing)

                    await asyncio.sleep(0.05)

                # Unsubscribe cleanly
                if streams:
                    await client.send(Unsubscribe(streams=streams))
                    print(f"[ws:{name}] unsubscribed")

                msg_task.cancel()
                print(f"[ws:{name}] CLOSE")

        except Exception as e:
            print(f"[ws:{name}] ERROR: {e} — reconnecting in 1s")

        await asyncio.sleep(1)

    print(f"[ws:{name}] stop requested → EXITING")

# ===============================================================
# Inbound message consumer
# ===============================================================
@dataclass(slots=True)
class Store:
    payments: list[dict[str, Any]] = field(default_factory=list)
    keys: dict[str, str] = field(default_factory=dict)
    last_seen_ledger: dict = field(default_factory=dict)

async def ws_consumer(
    inbound: asyncio.Queue,
    stop: asyncio.Event,
    store: Store,
    app: FastAPI,
):
    """Central message consumer.
    Every WSMessage from every listener goes through here.
    Route by stream type.
    """

    while not stop.is_set():
        try:
            msg: WSMessage = await inbound.get()

            # Debug print (optional)
            # print(f"[consume:{msg.listener}] {msg.stream}  queue={inbound.qsize()}")

            # print("CONSUMER GOT:", flush=True)
            # print(json.dumps(msg.stream, indent=2))
            # print(json.dumps(msg.payload, indent=2))
            if msg.stream == "ledgerClosed":
                # print(f"Saw ledgerClosed", flush=True)
                lsl = msg.payload.get("ledger_index")
                # print(f"lsl: {lsl}", flush=True)
                # print(f"store.last_seen_ledger: {store.last_seen_ledger}", flush=True)

                if store.last_seen_ledger.get("ledger") != lsl:
                    store.last_seen_ledger = {"ledger": lsl, "val": msg.listener}
                    print(f"logged {lsl} from {msg.listener}")
                    await schedule_payment_for_all_validators(app)
                else:
                    pass
                    # print(f"Ledger already seen {store.last_seen_ledger["ledger"]}")
            if msg.stream == "consensusPhase":
                log_consensus(msg)

            # if msg.stream:
            # if msg.stream == "consensusPhase":
        # store.consensus_count += 1
                # print("CONSENSUS reached:")
                # print(json.dumps(msg.payload, indent=2))
            # if msg.stream == "validationReceived":
            #     print(json.dumps(msg.payload, indent=2))
            # if msg.stream == "ledgerClosed":
            #     store.on_ledger_closed(msg.payload)
            # elif msg.stream == "transaction":
            #     store.on_transaction(msg.payload)
            # elif msg.stream == "validationReceived":
            #     store.on_validation(msg.payload)
            # elif msg.stream == "path_find":
            #     store.on_pathfind(msg.payload)
            # else:
            #     store.on_misc(msg.payload)

            inbound.task_done()
        except Exception as e:
            print("CONSUMER ERROR:", e)
        # while True:
        #     try:
        #         msg = inbound.get_nowait()
        #     except asyncio.QueueEmpty:
        #         break
        #     # await process_msg(msg, store)
        #     inbound.task_done()

async def heartbeat(app):
    address = genesis_account.get("address")
    destination = "roWE9i6HqqzcKHBJpHyZyubMVgP4a9PA3"
    private_key = genesis_account.get("private_key")
    public_key = genesis_account.get("public_key")
    amount =  "1000000"
    client = app.state.rpc_client

    while not app.state.stop.is_set():
        print("tick", flush=True)
        fee = await get_fee(client)
        seq = await get_next_valid_seq_number(address, client)

        payment = {
            "transaction_type": "Payment",
            "account": address,
            "destination": destination,
            "amount": amount,
            "fee": fee,
            "signing_pub_key": public_key,
            "sequence": seq,
        }
        pxrpl = transaction_json_to_binary_codec_form(payment)
        serialized_for_signing = encode_for_signing(pxrpl)
        serialized_bytes = bytes.fromhex(serialized_for_signing)
        signature = keypairs_sign(serialized_bytes, private_key)
        pxrpl["TxnSignature"] = signature
        pser = encode(pxrpl)
        req = SubmitOnly(tx_blob=pser)
        response = await client.request(req)
        print(f'tock {response.result["tx_json"]["hash"]}')
        erc = response.result["engine_result_code"]
        if erc:
            print("uh-oh")
        always(bool(erc), response.result["engine_result"], response.result["tx_json"])
        await asyncio.sleep(2)


# ===============================================================
# Manager to initialize everything inside FastAPI lifespan
# ===============================================================

async def init_ws_system(app):
    """Call this inside your lifespan block.
    Sets up:
        - inbound queue
        - outbound queue
        - stop event
        - TaskGroup for listeners + consumer
    """

    app.state.ws_inbound = asyncio.Queue(maxsize=5000)
    app.state.ws_outbound = asyncio.Queue(maxsize=5000)
    app.state.ws_stop = asyncio.Event()
    app.state.ws_listeners = {}

    inbound = app.state.ws_inbound
    outbound = app.state.ws_outbound
    stop = app.state.ws_stop
    store = app.state.store
    tg = app.state.tg

    # Spawn the consumer
    tg.create_task(ws_consumer(inbound, stop, store, app))
    print("[manager] WS system initialized")

def add_ws_listener(app, name: str, url: str, streams: list[StreamParameter]):
    """Add a new listener dynamically from a FastAPI route."""

    inbound = app.state.ws_inbound
    outbound = app.state.ws_outbound
    stop = app.state.ws_stop
    tg = app.state.tg

    task = tg.create_task(
        ws_listener(
            name=name,
            url=url,
            streams=streams,
            inbound=inbound,
            outbound=outbound,
            stop=stop,
        )
    )

    app.state.ws_listeners[name] = {
        "url": url,
        "streams": streams,
        "task": task,
    }

    print(f"[manager] listener added: {name} → {url} streams={streams}")

class Txn(BaseModel):
    account: str
    timestamp: datetime
    secret: str | None = None

class Pay(Txn):
    destination: str
    amount: int | dict | None = None

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
        print("WS raw (non-JSON): %s", raw_msg[:200])
        await queue.put(("raw", raw_msg))
        return

    msg_type = obj.get("type")
    print(msg_type)



@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.store = Store()
    app.state.listeners = {}
    app.state.stop = asyncio.Event()
    app.state.ws_inbound = asyncio.Queue(maxsize=5000)
    app.state.ws_outbound = asyncio.Queue(maxsize=5000)

    app.state.rpc_client = AsyncJsonRpcClient("http://rippled:5005")

    async with asyncio.TaskGroup() as tg:
        app.state.tg = tg
        tg.create_task(heartbeat(app))
        # await init_ws_system(app)
        # streams = [
        #     StreamParameter.CONSENSUS,
        #     StreamParameter.LEDGER,
        #     # StreamParameter.MANIFESTS,
        #     # StreamParameter.PEER_STATUS,
        #     # StreamParameter.SERVER,
        #     # StreamParameter.TRANSACTIONS,
        #     # StreamParameter.TRANSACTIONS_PROPOSED,
        #     StreamParameter.VALIDATIONS,
        # ]
        # # Example: start a listener at startup
        # for i in range(5):
        #     add_ws_listener(app, name=f"val{i}", url=f"ws://val{i}:6006", streams=streams)
        # url = "ws://rippled:6006"
        # async with AsyncWebsocketClient(url) as client:
        #     # using helper functions
        #     fee = await get_fee(client)
        #     print(f"1. get da fee: {fee}", flush=True)

        #     # using raw requests yourself
        #     fee = await client.request(Fee())
        #     print(f"2. get da fee: {fee}", flush=True)

        yield
        app.state.ws_stop.set()

app = FastAPI(
    title="Simple App",
    debug=True,
    lifespan=lifespan,
)


async def on_message(client):
    async for message in client:
        print(message)


@app.get("/q")
async def get_q():
    print(app.state.ws_queue.qsize())

# add a listener to a ws strema
@app.post("/sub/{host}/{stream}")
async def sub(host, stream):
    stre = StreamParameter.LEDGER
    print(f"subscribing to {stre}")
    req = Subscribe(streams=[stre])
    async with AsyncWebsocketClient(f"ws://{host}:6006") as client:
        listener = asyncio.create_task(on_message(client))
        await client.send(req)
        # yield
        # while client.is_open():
        #     await asyncio.sleep(0)
        # listener.cancel()

@app.post("/pay/")
async def do_pay(pmt: Pay):
    print(f"pmt: {pmt}")
    json_compatible_item_data = jsonable_encoder(pmt)
    app.state.store.payments.append(json_compatible_item_data)
    return 200

@app.get("/wallet/")
async def create_wallet():
    seed = generate_seed()
    print(f"seed: {seed}")
    pubkey, privkey = derive_keypair(seed)
    account_id = derive_classic_address(pubkey)
    print(f"account_id: {account_id}")
    x_address = classic_address_to_xaddress(account_id, 1, True)
    print(f"x_address: {x_address}")

    # json_compatible_item_data = jsonable_encoder(pmt)
    # app.state.store.payments.append(json_compatible_item_data)
    return 200

@app.get("/accounts/{addr}")
def find_address(addr: str):
    for p in app.state.store.payments:
        print(p)
        if p['account'] == addr:
            print(f"Found {addr}!")
            return 200
    print(f"{addr} not found!")
    return 404

@app.get("/listen/{host}")
def subscribe_to_stream(host: str):
    listener = listen(host)
    app.state.listeners[host] = listener
    print(f"added {host} to listeners")

import httpx
import json
import os
import time
from pathlib import Path
from typing import Any

import uvicorn
import xrpl
import copy
from xrpl.models.transactions import NFTokenMint, NFTokenMintFlag
from fastapi import Request

from workload import logger, utils
from workload.create import generate_wallets, generate_wallet_from_seed
from workload.check_xrpld_sync_state import is_xrpld_synced # TODO:git use xrpld_sync.py
from workload.config import conf_file, config_file
from workload.models import UserAccount
from workload.nft import mint_nft
from workload.randoms import sample, choice
from workload.txn_factory import generate_txn
from antithesis import lifecycle
from asyncio import TaskGroup
from fastapi import FastAPI, Depends
from workload import txn_factory
from xrpl.asyncio.account import get_next_valid_seq_number
from xrpl.asyncio.clients import AsyncJsonRpcClient
from xrpl.asyncio.ledger import get_latest_validated_ledger_sequence
from xrpl.asyncio.transaction import submit_and_wait, autofill_and_sign, sign_and_submit, submit
from xrpl.constants import CryptoAlgorithm
from xrpl.core.binarycodec import encode_for_signing, encode
from xrpl.core.keypairs import sign
from xrpl.models import IssuedCurrency, IssuedCurrencyAmount
from xrpl.models.transactions import (
    AccountSetAsfFlag,
    AccountSet,
    AccountSetFlag,
    TrustSet,
    TrustSetFlag,
    PaymentFlag,
    NFTokenCreateOffer,
    NFTokenCreateOfferFlag,
    NFTokenBurn,
    Payment,
    )
from xrpl.models.requests import ServerInfo, Fee
from xrpl.models.response import ResponseStatus
import time
from xrpl.wallet import Wallet

from dataclasses import dataclass, field

DEFAULT_BALANCE = conf_file["workload"]["accounts"]["default_balance"]
DEFAULT_PAYMENT = conf_file["workload"]["transactions"]["payment"]["default_amount"]


@dataclass
class AccountGenerator:
    source: Wallet
    client: AsyncJsonRpcClient
    default_balance: int = field(default=DEFAULT_PAYMENT)

    async def generate_accounts(self,
                                num_accounts: int,
                                amount: int | None = None,
                                wait=True
                                ):
        submit_method = submit_and_wait if wait else sign_and_submit
        # submit_method = submit_and_wait if wait else submit
        amount = amount or self.default_balance
        txns = []
        wallets = generate_wallets(num_accounts)
        seq = await get_next_valid_seq_number(address=self.source.address, client=self.client)
        wallet_list = list(enumerate(wallets))
        for idx, wallet in wallet_list :
            if self.source.address == wallet.address:
                logger.exception("Generated source and destination same") # TODO: Fix selecting src != dst
                continue
            txns.append(Payment(
                account=self.source.address,
                destination=wallet.address,
                amount=str(amount),
                sequence=seq + idx
            ))

        tasks = []
        start = time.time()

        try:
            async with TaskGroup() as tg:
                for txn in txns:
                    tasks.append(
                        tg.create_task(
                            submit_method(transaction=txn, client=self.client, wallet=self.source)
                        )
                    )
            responses = [t.result() for t in tasks]
            elapsed = time.time() - start
            logger.debug(f"wait took: {elapsed}")
            return zip(responses, wallets)
        except* Exception as eg:
            for e in eg.exceptions:
                logger.exception("handled other error:", e)

class Workload:
    def __init__(self, conf: dict[str, Any]):
        self.config = conf
        self.accounts = {}
        # TODO: Lookup account by nfts owned, tickets, etc
        self.gateways = []
        self.amms = []
        self.nfts = []
        self.currencies = []
        self.funding_wallet: Wallet = None
        self.failures = []
        self.currency_codes = conf["currencies"]["codes"]
        self.default_balance = conf["accounts"]["default_balance"]
        self.start_time = time.time()
        xrpld_host = os.environ.get("XRPLD_NAME", conf["xrpld"]["local"])
        xrpld_rpc_port = os.environ.get("XRPLD_RPC_PORT", conf["xrpld"]["json_rpc_port"])
        self.xrpld = f"http://{xrpld_host}:{xrpld_rpc_port}"
        logger.info("Connecting to xrpld at: %s", self.xrpld)
        use_ledger = False
        # if Path("/.dockerenv").is_file() and use_ledger:
        accounts_json = Path("/accounts.json")
        # else:
            # accounts_json = "/home/emel/dev/Ripple/rippled-antithesis/rippled-workload/testnet/accounts.json"
            # accounts_json = input("Enter path of accounts.json")
        if use_ledger:
            self.load_initial_accounts(accounts_json)
            fw = self.accounts[list(self.accounts)[0]]
            logger.info(f"Using {fw} for funding account")
            self.funding_wallet = fw.wallet
            # self.funding_wallet = generate_wallet_from_seed(self.config["genesis_account"]["master_seed"])
        else:
            self.funding_wallet = generate_wallet_from_seed(self.config["genesis_account"]["master_seed"])
        self.client = AsyncJsonRpcClient(self.xrpld)
        self.account_generator = AccountGenerator(source=self.funding_wallet, client=self.client)

        self.wait_for_network(self.xrpld)

        workload_ready_msg = "Workload initialization complete"
        logger.info("%s after %ss", workload_ready_msg, int(time.time() - self.start_time))
        lifecycle.setup_complete(details={"message": workload_ready_msg})
        # print('{"antithesis_setup": { "status": "complete", "details": "" }}')
        # logger.info("Called lifecycle setup_complete()")

    @property
    def addresses(self):
        return list(self.accounts.keys())

    async def generate_accounts(self, n: int, wait=True):
        logger.debug("Starting generate accounts()")
        olf = await self.fee()
        open_ledger_fee = int(olf["drops"]["open_ledger_fee"])
        for result, wallet in await self.account_generator.generate_accounts(n, wait=wait):
            if result.status == ResponseStatus.SUCCESS:
                account = UserAccount(wallet)
                self.accounts[account.address] =  account
            else:
                self.failures.append(wallet)
        fee_paid = int(result.result["tx_json"]["Fee"])
        if fee_paid > open_ledger_fee:
            logger.debug(f"Paid {fee_paid=} vs {open_ledger_fee=}")
        logger.debug(f"{len(self.addresses)} accounts")

    async def server_info(self):
        server_info_response = await self.client.request(ServerInfo())
        return server_info_response.result["info"]

    async def fee(self):
        fee_response = await self.client.request(Fee())
        return fee_response.result

    async def get_ref_fee(self):
        fee_response = await self.fee()
        return fee_response["drops"]["base_fee"]

    async def get_open_ledger_fee(self):
        fee_response = await self.fee()
        return fee_response["drops"]["open_ledger_fee"]

    async def expected_ledger_size(self):
        fee_response = await self.fee()
        return int(fee_response["expected_ledger_size"])

    def load_initial_accounts(self, accounts_json):
        # TODO: Doesn't need to be in workload
        try:
            accounts_json = Path(accounts_json)
            logger.info(f"Loading accounts from {accounts_json}")
            accounts = json.loads(accounts_json.read_text())
        except FileNotFoundError:
            logger.error("accounts.json not found.")
            if True: # TODO: Fix this for some kind of local testing
                local_path = "accounts.json"
            # local_path = input("Enter local file path:")
            accounts_json = Path(local_path)
            logger.info(f"Using accounts.json at: {accounts_json.resolve()}")
            accounts = json.loads(accounts_json.read_text())
            logger.info(f"{len(accounts)} accounts found!")
            for idx, i in enumerate(accounts):
                logger.debug(f"{idx}: {i}")
        self.account_data = accounts

        default_algo = CryptoAlgorithm[conf_file["workload"]["accounts"]["default_crypto_algorithm"]]

        def generate_wallet_from_seed(seed: str, algorithm: CryptoAlgorithm = default_algo) -> Wallet:
            wallet = Wallet.from_seed(seed=seed, algorithm=algorithm)
            return wallet
        for _, seed in self.account_data:
            wallet = generate_wallet_from_seed(seed)
            self.accounts[wallet.address] = UserAccount(wallet=wallet)
        logger.info(f"Loaded {len(self.accounts)} initial accounts")

    @classmethod
    def issue_currencies(cls, issuer: str, currency_code: list[str]) -> list[IssuedCurrency]:
        """Use a fixed set of currency codes to create IssuedCurrencies for a specific gateway.

        Args:
            issuer (str): Account_id of the gateway for all currencies
            currency_code (list[str], optional): _description_. Defaults to config.currency_codes.

        Returns:
            list[IssuedCurrency]: List of IssuedCurrencies a gateway provides

        """
        issued_currencies = [IssuedCurrency.from_dict(dict(issuer=issuer, currency=cc)) for cc in currency_code]
        logger.debug("Issued %s currencies", len(issued_currencies))
        if True:
            for c in issued_currencies:
                logger.debug(c)
        return issued_currencies

    def wait_for_network(self, xrpld) -> None:
        timeout = self.config["xrpld"]["timeout"]  # Wait at most 10 minutes
        wait_start = time.time()
        logger.debug("Waiting %ss for xrpld at %s to be running.", timeout, xrpld)
        while not (is_xrpld_synced(xrpld)):
            irs = is_xrpld_synced(xrpld)
            logger.info(f"is_xrpld_synced returning: {irs}")

            if (xrpld_ready_time := int(time.time() - self.start_time)) > timeout:
                logger.info("xrpld ready after %ss", xrpld_ready_time)
            logger.info("Waited %ss so far", int(time.time() - wait_start))
            wait_time = 10
            time.sleep(wait_time)
        logger.info("xrpld ready...")

    async def gen_payment(self, src, dst, amount):
        source = self.accounts[src]
        seq = await get_next_valid_seq_number(address=src, client=self.client)
        txn = Payment(
            account=source.address,
            destination=dst,
            amount=amount,
            sequence=seq,
        )
        return txn

    async def submit_txn(self, txn, wait=True):
        logger.info(txn)
        submit_method = submit_and_wait if wait else sign_and_submit
        source = self.accounts[txn.account]
        logger.info(source)
        wallet = source.wallet
        response = await submit_method(transaction=txn, client=self.client, wallet=wallet)
        logger.info(response)

    async def make_payment(self, payment_data):
        logger.info("hit make_payment()")
        logger.info(f"{payment_data=}")
        account = payment_data.account
        destination = payment_data.destination
        amount = payment_data.amount
        txn = await self.gen_payment(account, destination, str(amount))
        result = await self.submit_txn(txn)
        logger.info(f"got result: {result}")

    async def submit_payments(self, n: int, wallet: Wallet, destination_address: str):
        seq = await get_next_valid_seq_number(wallet.address, client=self.client)
        latest_ledger = await get_latest_validated_ledger_sequence(client=self.client)

        signed_blobs = []
        for i in range(n):
            tx_json = Payment(
                account=wallet.address,
                amount="1000000",
                destination=destination_address,
                sequence=seq + i,
                fee="500",
                last_ledger_sequence=latest_ledger + 10,
                signing_pub_key=wallet.public_key
            ).to_xrpl()

            signing_blob = encode_for_signing(tx_json)
            signature = sign(signing_blob, wallet.private_key)
            tx_json["TxnSignature"] = signature

            signed_blob = encode(tx_json)
            signed_blobs.append(signed_blob)

        responses = []
        async with TaskGroup() as tg:
            for blob in signed_blobs:
                tg.create_task(self.submit_via_http(blob, responses))

        return responses

    async def submit_via_http(self, blob: str, responses: list):
        # TODO: Remove this
        payload = {
            "method": "submit",
            "params": [{"tx_blob": blob}]
        }
        async with httpx.AsyncClient() as client:
            try:
                resp = await client.post(self.xrpld, json=payload)
                responses.append(resp.json())
            except Exception as e:
                responses.append({"error": str(e)})

    async def pay(self):
        src, dst = sample(self.account_data, 2)
        src_secret = src[1]
        dst_address = dst[0]
        src_wallet = Wallet.from_secret(src_secret, algorithm=CryptoAlgorithm.SECP256K1)
        responses = await self.submit_payments(100, src_wallet, dst_address)
        for i in responses:
            print(i)

        return {"cool": "beans"}

    async def mint_random_nft(self):
        account_id = choice(list(self.accounts))
        account = self.accounts[account_id]
        sequence = await get_next_valid_seq_number(account.address, self.client)
        result = await mint_nft(account, sequence, self.client)
        logger.info(json.dumps(result, indent=2))
        logger.info(json.dumps(result["meta"]["nftoken_id"], indent=2))
        logger.info(json.dumps(result["tx_json"]["Account"], indent=2))
        nft_owner = result["tx_json"]["Account"]
        nftoken_id = result["meta"]["nftoken_id"]
        from workload.models import NFT
        nft = NFT(owner=account.address, nftoken_id=nftoken_id)
        self.nfts.append(nft)
        account.nfts.add(nftoken_id)
        # for a in self.accounts:
        #     if a.address == nft_owner:
        #         nft = NFT(owner=a, nftoken_id=nftoken_id)
        #         self.nfts.append(nft)
        #         logger.info(f"Added NFT {nftoken_id} with ownder {a}")
        #         break

        logger.info(f"Account {account.address}'s NFTs:")
        for idx, nft in enumerate(account.nfts):
            logger.info(f"{idx}: {nft}")

    def get_accounts(self):
        return self.accounts

    def get_nfts(self):
        return self.nfts

    async def nftoken_create_offer(self, account, nft_id, wallet):
        create_amount = "1000000"
        logger.debug("Creating offer for %s's nft [%s]", account, nft_id)
        nftoken_offer_create_txn = NFTokenCreateOffer(
            account=account,
            nftoken_id=nft_id,
            amount=create_amount,
            flags=NFTokenCreateOfferFlag.TF_SELL_NFTOKEN,
        )
        logger.debug(json.dumps(nftoken_offer_create_txn.to_xrpl(), indent=2))
        nftoken_offer_create_txn_response = await submit_and_wait(transaction=nftoken_offer_create_txn, client=self.client, wallet=wallet)
        return nftoken_offer_create_txn_response.result

    async def create_random_nft_offer(self):
        if not self.nfts:
            logger.info("No NFTs to make offers on!")
            return
        nft = choice(self.nfts)
        owner = self.accounts[nft.owner]
        res = await self.nftoken_create_offer(owner.address, nft.nftoken_id, owner.wallet)
        logger.debug(res)
        return None

    async def nft_burn_random(self):
        if not self.nfts:
            logger.debug("No NFTs to burn!")
            return
        nft = choice(self.nfts)
        nft_owner = nft.owner
        nft_owner = self.accounts[nft_owner]
        nftburn_txn = NFTokenBurn(account=nft_owner.address, nftoken_id=nft.nftoken_id)
        nftburn_txn_response = await submit_and_wait(transaction=nftburn_txn, client=self.client, wallet=nft_owner.wallet)
        return nftburn_txn_response.result

    async def payment_random(self):
        amount = str(1_000_000)
        src_address, dst = sample(list(self.accounts), 2)
        sequence = await get_next_valid_seq_number(src_address, self.client)
        src = self.accounts[src_address]
        payment_txn = Payment(account=src.address, amount=amount, destination=dst, sequence=sequence)
        response = await sign_and_submit(payment_txn, self.client, src.wallet)
        logger.debug("Payment from %s to %s for %s submitted.", src.address, dst, amount)
        return response, src.address, dst, amount

    async def create_ticket(self):
        ticket_count = 5
        logger.debug(choice(list(self.accounts)))
        account_id  = choice(list(self.accounts))
        logger.debug(f"{account_id=}")
        account = self.accounts[account_id]
        logger.debug(f"Chose account: {account}")
        ticket_create_txn = xrpl.models.TicketCreate(
            account=account.address,
            ticket_count=ticket_count,
        )
        response = await submit_and_wait(ticket_create_txn, self.client, account.wallet)
        result = response.result
        logger.debug(json.dumps(result, indent=2))

        ticket_seq = result["tx_json"]["Sequence"] + 1
        tix = [ticket_seq for ticket_seq in range(ticket_seq, ticket_seq + ticket_count)]
        account.tickets.update(tix)
        logger.debug(f"Account {account.address} tickets: {account.tickets=}")
        # logger.info(f"Created tickets: {tickets}")
        # self.accounts
        return None

    async def use_random_ticket(self):
        account_ids = self.addresses
        len_account_ids = len(account_ids)
        logger.debug(f"Length of account_ids: {len_account_ids}")
        for aid in account_ids:
            logger.debug(f"{self.accounts[aid].tickets}")
            len_tickets = len(self.accounts[aid].tickets)
            if len_tickets > 0:
                account_id = aid
                logger.debug(f"Found {aid} to have {len_tickets} tickets {self.accounts[aid].tickets}")
                break
            else:
                logger.debug(f"removing {aid} from list")
                account_ids.remove(aid)
                logger.debug(f"list now {len(account_ids)} long")

        # Our ticket holder is the source account
        src = self.accounts[account_id]
        logger.debug(f"Ticket holder: {src.address}")
        # Use a random ticker of theirs
        ticket_sequence = choice(list(src.tickets))
        logger.debug(f"Using ticket: {ticket_sequence}")
        # Pick a destination account that's not the source
        account_ids = self.addresses
        account_ids.remove(account_id)
        dst = choice(account_ids)
        amount = str(1_000_000)
        payment_txn = Payment(
            account=src.address,
            destination=dst,
            amount=amount,
            sequence=0,
            ticket_sequence=ticket_sequence,
        )
        result = await submit_and_wait(payment_txn, self.client, src.wallet)
        logger.debug(result)
        return result

    async def random_batch(self):
        from xrpl.models import Batch, BatchFlag, Payment
        amount = 1_000_000
        src_address, dst = sample(list(self.accounts), 2)
        sequence = await get_next_valid_seq_number(src_address, self.client)
        src = self.accounts[src_address]
        num_txns = 8
        batch_flag = choice(list(BatchFlag))
        logger.info(f"Submitting Batch txn {batch_flag.name} ")
        raw_transactions = [Payment(
            account=src.address,
            # amount=("1000000" if idx < 3 else "10000000000000"), # have a until_failure fail
            amount=str(amount),
            flags=xrpl.models.TransactionFlag.TF_INNER_BATCH_TXN,
            destination=dst,
            sequence=sequence + idx + 1,
            fee="0",
            signing_pub_key=""
        ) for idx in range(num_txns)]

        batch_txn = Batch(
            account=src.address,
            flags=batch_flag,
            raw_transactions=[*raw_transactions],
            sequence=sequence,
        )

        response = await submit_and_wait(batch_txn, self.client, src.wallet)
        result = response.result
        logger.info(json.dumps(result, indent=2))

    async def mpt_create(self):
        from xrpl.models import MPTokenIssuanceCreate
        # src_address, dst = sample(list(self.accounts), 2)
        src_address = choice(list(self.accounts))
        sequence = await get_next_valid_seq_number(src_address, self.client)
        src = self.accounts[src_address]

        mpt_txn = MPTokenIssuanceCreate(
            account=src.address,
            # asset_scale="2",
            # maximum_amount="100000000",
            # mptoken_metadata=b"cool".hex()
        )
        response = await submit_and_wait(mpt_txn, self.client, src.wallet)
        result = response.result
        logger.info(json.dumps(result, indent=2))

    # async def make_request(self, request):
    #     logger.info(f"got request: {request}")
    #     payload = {"method": "server_info"}
    #     logger.info(f"self.rippled: {self.rippled}")
    #     response = httpx.post(self.rippled, json=payload)
    #     logger.info(response.text)
    #     res = response.json()
    #     logger.info(res)
    #     return res

    async def test_txn_factory(self, singular=False):
        ctx = txn_factory.TxnContext(
            account=self.funding_wallet.address,
            fee=await self.get_ref_fee(),
            addresses=self.addresses,
            )
        if not self.addresses:
            return
        potential_destinations = copy.copy(self.addresses)
        if ctx.account in potential_destinations:
            potential_destinations.remove(ctx.account)
        destination = choice(potential_destinations)
        ica = IssuedCurrencyAmount(
            issuer=destination,
            currency=choice(self.currency_codes),
            value=self.config["transactions"]["trustset"]["limit"],
        )
        seq = await get_next_valid_seq_number(
                    address=self.funding_wallet.address,
                    client=self.client
                    )
        aset = generate_txn("AccountSet", ctx,
                            SetFlag=AccountSetAsfFlag.ASF_DEFAULT_RIPPLE,
                            Sequence=seq,
                            )
        ts = generate_txn("TrustSet", ctx,
                      LimitAmount=ica,
                      Sequence=seq + 1
                      )
        p = generate_txn("Payment", ctx,
                         Amount=DEFAULT_PAYMENT,
                         Sequence=seq + 2
                         )
        n = generate_txn("NFTokenMint", ctx,
                         Sequence=seq + 3,
                         )
        txns = [aset, ts, p, n]
        signed_txns = [await autofill_and_sign(transaction=txn, client=self.client, wallet=self.funding_wallet) for txn in txns]
        responses = []
        if singular:
            for txn in signed_txns:
                response = await submit_and_wait(transaction=txn, client=self.client, wallet=self.funding_wallet)
                responses.append(response)
        else:
            start = time.time()
            tasks = []
            try:
                async with TaskGroup() as tg:
                    for txn in signed_txns:
                        tasks.append(
                            tg.create_task(
                                submit(transaction=txn, client=self.client)
                            )
                        )
                responses = [t.result() for t in tasks]
                elapsed = time.time() - start
                # return zip(responses, wallets)
            except* Exception as eg:
                for e in eg.exceptions:
                    print("handled other error:", e)

def create_app(workload: Workload) -> FastAPI:
    app = FastAPI()
    from pydantic import BaseModel

    # class Item(BaseModel):
    #     name: str
    #     description: str | None = None
    #     price: float
    #     tax: float | None = None
    # class PaymentM(BaseModel):
    #     default_amount: int = 100
    #     account: str
    #     destination: str
    #     amount: int = default_amount
    # class NumAccounts(BaseModel):
    #     n: int = 10

    # @app.post("/payment1")
    # async def payment1(data: Request):
    #     try:
    #         res = await data.json()
    #     except Exception as ex:
    #         res = str(ex)
    #     logger.info(res)
    #     return res

    def get_workload():
        return workload

    @app.get("/mix")
    async def mix(w: Workload = Depends(get_workload)):
        try:
            await w.test_txn_factory()
        except Exception as e:
            logger.error(f"txn_factory failed: {type(e).__name__}: {e}")

    @app.get("/generate_accounts")
    async def generate_accounts(w: Workload = Depends(get_workload)):
    # async def generate_accounts(data: Request, w: Workload = Depends(get_workload)):
        # res = await data.json()
        try:
            await w.generate_accounts(100)
        except Exception as e:
            logger.error(f"generate_accounts failed: {type(e).__name__}: {e}")

    @app.get("/accounts")
    def get_accounts(w: Workload = Depends(get_workload)):
        accounts = w.addresses
        for a in accounts:
            logger.info(a)
        return accounts

    @app.get("/nft/list")
    def get_nfts(w: Workload = Depends(get_workload)):
        nfts = w.get_nfts()
        for n in nfts:
            logger.info(n)

    @app.get("/nft/mint/random")
    async def mint_random_nft(w: Workload = Depends(get_workload)):
        return await w.mint_random_nft()

    @app.get("/nft/create_offer/random")
    async def create_random_nft_offer(w: Workload = Depends(get_workload)):
        return await w.create_random_nft_offer()

    @app.get("/nft/burn/random")
    async def burn_nft(w: Workload = Depends(get_workload)):
        return await w.nft_burn_random()

    @app.get("/pay")
    async def payment_random(w: Workload = Depends(get_workload)):
        return await w.pay()

    @app.get("/payment/random")
    async def make_payment(w: Workload = Depends(get_workload)):
        return await w.payment_random()

    @app.get("/tickets/create/random")
    async def create_ticket(w: Workload = Depends(get_workload)):
        return await w.create_ticket()

    @app.get("/tickets/use/random")
    async def use_random_ticket(w: Workload = Depends(get_workload)):
        return await w.use_random_ticket()

    @app.get("/batch/random")
    async def batch(w: Workload = Depends(get_workload)):
        return await w.random_batch()

    @app.get("/mpt/create") # TODO: mpt/issuance/create
    async def mpt_create(w: Workload = Depends(get_workload)):
        return await w.mpt_create()

    @app.get("/request")
    async def request(w: Workload = Depends(get_workload), body: str = ""):
        return await w.make_request(body)
    ## This requires issued currencies
    # @app.get("/offers/create/random")
    # async def cancel_create_random_offer(w: Workload = Depends(get_workload)):
    #     return await w.cancel_create_random_offer()
    # @app.get("/offers/cancel/random")
    # async def cancel_random_offer(w: Workload = Depends(get_workload)):
    #     return await w.cancel_random_offer()
    return app

def main():
    logger.info("Loaded config from %s", config_file)
    conf = conf_file["workload"]
    logger.info("Config %s", json.dumps(conf, indent=2))
    workload = Workload(conf)
    app = create_app(workload)
    uvicorn.run(app, host="0.0.0.0", port=8000)

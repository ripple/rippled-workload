from random import SystemRandom

from xrpl.account import get_next_valid_seq_number
from xrpl.clients import JsonRpcClient
from xrpl.constants import CryptoAlgorithm
from xrpl.core import keypairs
from xrpl.models.transactions import Payment
from xrpl.transaction import sign_and_submit
from xrpl.wallet import Wallet
from workload.config import conf_file
from workload import logger

urand = SystemRandom()  # TODO: import from module
randrange = urand.randrange
sample = urand.sample

default_algo = CryptoAlgorithm.SECP256K1
seed = keypairs.generate_seed(algorithm=default_algo)
public, private = keypairs.derive_keypair(seed)
test_account = keypairs.derive_classic_address(public)

default_balance = conf_file["workload"]["accounts"]["default_balance"]


# TODO: Use from config
genesis_account = {
    "account_id": "rHb9CJAWyB4rj91VRWn96DkukG4bwdtyTh",
    "master_seed": "snoPBrXtMeMyMHUVTgbuqAfg1SUTb",
}

funding_account = Wallet.from_seed(seed=genesis_account["master_seed"], algorithm=default_algo)


def generate_wallet(n: int = 1, algorithm: CryptoAlgorithm = default_algo) -> list[Wallet]:
    wallets = []
    for _ in range(n):
        wallet = Wallet.from_seed(seed=keypairs.generate_seed(algorithm=algorithm))
        logger.debug("wallet_propose: %s", f"{wallet.address} {wallet.seed}")
        wallets.append(wallet)
    return wallets


def create_accounts(number: int, client: JsonRpcClient, amount: str = default_balance):
    """Create Accounts.

    Args:
        number (int): _description_
        client (JsonRpcClient): _description_
        amount (str, optional): _description_. Defaults to default_balance.

    Does _not_ guarantee accouts are actually created on ledger but returns credentials for them and results

    Returns:
        _type_: [list[Wallet], Response]]

    """
    payment_txns = []
    wallets = generate_wallet(n=number)
    sequence = get_next_valid_seq_number(funding_account.address, client)
    for wallet in wallets:
        logger.info("Creating %s", wallet.address)
        payment_txns.append(Payment(
            account=funding_account.address,
            amount=amount,
            destination=wallet.address,
            sequence=sequence,
        ))
        sequence += 1  # time which is faster enumeratinging or +=1
    return wallets, [sign_and_submit(txn, client, funding_account) for txn in payment_txns]

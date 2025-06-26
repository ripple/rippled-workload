from xrpl.account import get_next_valid_seq_number
from xrpl.clients import JsonRpcClient
from xrpl.constants import CryptoAlgorithm
from xrpl.core import keypairs
from xrpl.models.transactions import Payment
from xrpl.transaction import sign_and_submit
from xrpl.wallet import Wallet
from workload.config import conf_file
from workload import logger

default_balance = conf_file["workload"]["accounts"]["default_balance"]
genesis_account = conf_file["workload"]["genesis_account"]
default_algo = CryptoAlgorithm[conf_file["workload"]["accounts"]["default_crypto_algorithm"]]

genesis_wallet = Wallet.from_seed(seed=genesis_account["master_seed"], algorithm=default_algo)


def generate_wallet_from_seed(seed: str, algorithm: CryptoAlgorithm = default_algo) -> Wallet:
    wallet = Wallet.from_seed(seed=seed, algorithm=algorithm)
    logger.debug("Wallet %s from seed %s. CryptoAlgorithm: [%s] Wallet.algorithm: [%s]", wallet, seed, algorithm.name, wallet.algorithm.name)
    return wallet

def generate_wallet(seed: str, algorithm: CryptoAlgorithm = default_algo) -> Wallet:
    return generate_wallet_from_seed(seed, algorithm)

def generate_wallets(n: int = 1, algorithm: CryptoAlgorithm = default_algo) -> list[Wallet]:
    wallets = []
    for _ in range(n):
        seed = keypairs.generate_seed(algorithm=algorithm)
        wallet = Wallet.from_seed(seed=seed, algorithm=algorithm)
        logger.debug("Wallet [%s] from seed [%s] using algorithm %s", wallet.classic_address, seed, algorithm.name)
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
    wallets = generate_wallets(n=number)
    sequence = get_next_valid_seq_number(genesis_wallet.address, client)
    for wallet in wallets:
        logger.debug("Creating %s", wallet.address)
        payment_txns.append(Payment(
            account=genesis_wallet.address,
            amount=amount,
            destination=wallet.address,
            sequence=sequence,
        ))
        sequence += 1
    return wallets, [sign_and_submit(txn, client, genesis_wallet) for txn in payment_txns]

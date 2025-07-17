import json
from pathlib import Path

from client import rippled, workload_json
from xrpl.models.requests import AccountNFTs
from xrpl.wallet import Wallet

from workload import logger
from workload.create import generate_wallet_from_seed

objects_dir = Path(workload_json).parent / "ledger_objects"
objects_dir.mkdir(exist_ok=True, parents=True)

alf = Path(workload_json).parent / "accounts_loaded"


async def get_all_nfts(account):
    return await rippled.request(AccountNFTs(account=account))


def get_initial_accounts():
    return json.loads(Path(workload_json).read_text(encoding="utf-8"))


def log_object(path: str, ledger_object: str) -> bool:
    object_record_path = objects_dir / path
    logger.debug("Logging to: %s", object_record_path)
    object_record_path.mkdir(exist_ok=True, parents=True)
    object_record = object_record_path / Path(ledger_object)
    object_record.touch()
    logger.debug("Created %s", object_record)
    created = object_record.is_file()
    if not created:
        logger.error(f"record {object_record} not created!")
    return created


def log_account(wallet: Wallet) -> bool:
    logger.debug("logging account %s", wallet.address)
    return log_object("accounts", f"{wallet.address}-{wallet.seed}")


def load_initial_workload_accounts():
    if alf.is_file():
        logger.info("Aleady read initial accounts")
        logger.info(alf.read_text())
        return
    logger.info("Loading accounts created during workload startup")
    initial_accounts = get_initial_accounts()
    for account in initial_accounts:
        account_wallet = generate_wallet_from_seed(account[1])
        log_account(account_wallet)
    alf.write_text("\n".join(" ".join(pair) for pair in initial_accounts) + "\n", encoding="utf-8")


def get_object(object_type: str):
    search_dir = objects_dir / object_type
    return list(search_dir.iterdir())


def get_nft_owners():
    try:
        owners_dir = get_object("nfts/owners")
        owners = [i.name for i in owners_dir]
        logger.debug("NFT owners: %s", owners)
        return owners
    except FileNotFoundError:
        return None


def get_nft_minters():
    nft_minters = get_object("nfts/owners")
    # logger.info(nft_minters)
    return [minter.name for minter in nft_minters]


def get_nfts(address: str) -> list[str]:
    nft_ids = get_object(f"nfts/owners/{address}")
    logger.debug("%s's nft ids:", address)
    for n in nft_ids:
        logger.debug("   %s", n.name)
    nft_ids = [nftid.name for nftid in nft_ids]
    return nft_ids


def get_nft_sell_offers() -> list[str]:
    try:
        nft_sell_offers_dir = get_object("nfts/offers/sell")
        offers = [offer.name for offer in nft_sell_offers_dir]
        logger.info("NFT Sell offers")
        for o in offers:
            logger.info("\t%s", o)
        return offers
    except FileNotFoundError:
        return []


def get_accounts():
    accounts_dir = get_object("accounts")
    accounts = [account.name for account in accounts_dir]
    logger.debug("Getting all known accounts")
    logger.debug(accounts)
    return accounts


def get_all_acccounts():
    """Populate the initial list of accounts from those created by the workload at startup.

    Returns:
        list: _description_
    """
    initial_accounts = get_initial_accounts()
    created_accounts = [a.name.split("-") for a in get_object("accounts")]
    return [*initial_accounts, *created_accounts]


def load_wallet_from_account(account):
    accounts = get_all_acccounts()
    for acct, seed in accounts:
        if acct == account:
            return generate_wallet_from_seed(seed)
    return None


def log_nft_offer_create(owner, nft_id, direction):
    return log_object(f"nfts/offers/{direction}", f"{owner}-{nft_id}")


def log_nft(wallet: Wallet, nft_id) -> None:
    logged = log_object(f"nfts/owners/{wallet.address}", nft_id)
    if not logged:
        logger.error("Couldn't log %s %s", wallet.address, nft_id)


def delete_nft(owner, nftid):
    owner_path = Path(objects_dir / "nfts/owners" / owner)
    nftid_path = owner_path / nftid
    logger.debug("Deleting %s", nftid_path)
    nftid_path.unlink()
    if not any(owner_path.iterdir()):
        owner_path.rmdir()


def remove_nft_sell_offer(owner, offer_id):
    offer_path = Path(objects_dir / "nfts/offers/sell" / f"{owner}-{offer_id}")
    logger.debug("Deleting %s", offer_path)
    offer_path.unlink()


def transfer_nft(from_, offer_id, to_, nft_id):
    log_nft(to_, nft_id)
    delete_nft(from_, nft_id)
    remove_nft_sell_offer(from_, offer_id )


if __name__ == "__main__":
    load_initial_workload_accounts()

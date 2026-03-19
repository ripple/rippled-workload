import json
import logging
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from random import choice, choices, sample
from typing import Any, TypeVar

from xrpl.models import IssuedCurrency, TransactionFlag
from xrpl.models.amounts import IssuedCurrencyAmount
from xrpl.models.transactions import (
    AccountSet,
    AMMCreate,
    AMMDeposit,
    AMMDepositFlag,
    AMMWithdraw,
    AMMWithdrawFlag,
    Batch,
    BatchFlag,
    CredentialAccept,
    CredentialCreate,
    CredentialDelete,
    DelegateSet,
    Memo,
    MPTokenAuthorize,
    MPTokenIssuanceCreate,
    MPTokenIssuanceDestroy,
    MPTokenIssuanceSet,
    NFTokenAcceptOffer,
    NFTokenBurn,
    NFTokenCancelOffer,
    NFTokenCreateOffer,
    NFTokenMint,
    OfferCancel,
    OfferCreate,
    Payment,
    PermissionedDomainDelete,
    PermissionedDomainSet,
    TicketCreate,
    Transaction,
    TrustSet,
    VaultClawback,
    VaultCreate,
    VaultDelete,
    VaultDeposit,
    VaultSet,
    VaultWithdraw,
)
from xrpl.models.transactions.delegate_set import GranularPermission
from xrpl.models.transactions.deposit_preauth import Credential as XRPLCredential
from xrpl.transaction import transaction_json_to_binary_codec_form
from xrpl.wallet import Wallet

from workload.randoms import randrange, random

log = logging.getLogger("workload.txn")

T = TypeVar("T")


def choice_omit(seq: Sequence[T], omit: Iterable[T]) -> T:
    pool = [x for x in seq if x not in omit]
    if not pool:
        raise ValueError("No options left after excluding omits!")
    return choice(pool)


AwaitInt = Callable[[], Awaitable[int]]
AwaitSeq = Callable[[str], Awaitable[int]]


@dataclass(slots=True)
class TxnContext:
    funding_wallet: "Wallet"
    wallets: Sequence["Wallet"]  # <-- sequence, not dict    currencies: Sequence[IssuedCurrency]
    currencies: Sequence[IssuedCurrency]
    config: dict  # Full config dict from config.toml
    base_fee_drops: "AwaitInt"
    next_sequence: "AwaitSeq"
    mptoken_issuance_ids: list[str] | None = None  # MPToken issuance IDs
    amm_pools: set[frozenset[str]] | None = None  # AMM pools (asset pairs) for dedup
    amm_pool_registry: list[dict] | None = None  # [{asset1: {...}, asset2: {...}, creator: str}]
    nfts: dict[str, str] | None = None  # NFTs: {nft_id: owner}
    offers: dict[str, dict] | None = None  # Offers: {offer_id: {type, owner, ...}}
    tickets: dict[str, set[int]] | None = None  # Tickets: {account: {ticket_seq, ...}}
    balances: dict[str, dict[str | tuple[str, str], float]] | None = None  # In-memory balance tracking
    disabled_types: set[str] | None = None  # Runtime-overridable disabled types
    forced_account: "Wallet | None" = None  # When set, rand_account() always returns this wallet as source
    credentials: list[dict] | None = None  # [{issuer, subject, credential_type, accepted}]
    vaults: list[dict] | None = None  # [{vault_id, owner, asset}]
    domains: list[dict] | None = None  # [{domain_id, owner}]

    def rand_accounts(self, n: int, omit: list[str] | None = None) -> list["Wallet"]:
        """Pick n unique random accounts, optionally excluding addresses.

        When forced_account is set, it is guaranteed to be the first element
        (unless its address is in omit, in which case it is excluded normally).
        """
        omit_set = set(omit) if omit else set()
        if self.forced_account is not None and self.forced_account.address not in omit_set:
            rest = [w for w in self.wallets if w.address != self.forced_account.address and w.address not in omit_set]
            if len(rest) < n - 1:
                raise ValueError(f"Need {n} accounts but only {len(rest) + 1} available after excluding {omit}")
            return [self.forced_account] + sample(rest, n - 1)
        available = [w for w in self.wallets if w.address not in omit_set]
        if len(available) < n:
            raise ValueError(f"Need {n} accounts but only {len(available)} available after excluding {omit}")
        return sample(available, n)

    def rand_account(self, omit: list[str] | None = None) -> "Wallet":
        """Pick a single random account, optionally excluding addresses.

        When forced_account is set, returns it unless its address is in omit.
        """
        return self.rand_accounts(1, omit)[0]

    def rand_owner(self, owner_addresses: set[str]) -> "Wallet | None":
        """Pick a random account that is in the owner set.

        When forced_account is set, returns it only if it's an owner (else None).
        When not forced, picks randomly from the intersection of wallets and owners.
        """
        if self.forced_account is not None:
            return self.forced_account if self.forced_account.address in owner_addresses else None
        candidates = [w for w in self.wallets if w.address in owner_addresses]
        return choice(candidates) if candidates else None

    def get_account_currencies(self, account: "Wallet") -> list[IssuedCurrency]:
        """Get list of IOU currencies this account has a non-zero balance of.

        Returns currencies the account can actually send (has positive balance).
        Useful for avoiding tecPATH_DRY errors.
        """
        if not self.balances or account.address not in self.balances:
            return []

        account_balances = self.balances[account.address]
        currencies_with_balance = []

        for key, balance in account_balances.items():
            if isinstance(key, tuple) and balance > 0:
                currency_code, issuer = key
                currencies_with_balance.append(IssuedCurrency(currency=currency_code, issuer=issuer))

        return currencies_with_balance

    def rand_currency(self) -> IssuedCurrency:
        if not self.currencies:
            raise RuntimeError("No currencies configured")
        return choice(self.currencies)

    def rand_mptoken_id(self) -> str:
        """Get a random MPToken issuance ID from tracked IDs."""
        if not self.mptoken_issuance_ids:
            raise RuntimeError("No MPToken issuance IDs available")
        return choice(self.mptoken_issuance_ids)

    def _asset_id(self, amount: str | dict) -> str:
        """Convert an Amount (XRP drops or IOU) to a unique asset identifier."""
        if isinstance(amount, str):
            return "XRP"
        else:
            return f"{amount['currency']}.{amount['issuer']}"

    def amm_pool_exists(self, asset1: str | dict, asset2: str | dict) -> bool:
        """Check if an AMM pool for this asset pair already exists."""
        if not self.amm_pools:
            return False
        id1 = self._asset_id(asset1)
        id2 = self._asset_id(asset2)
        pool_id = frozenset([id1, id2])
        return pool_id in self.amm_pools

    def rand_amm_pool(self) -> dict:
        """Pick a random AMM pool from the registry."""
        if not self.amm_pool_registry:
            raise RuntimeError("No AMM pools available")
        return choice(self.amm_pool_registry)

    def derive(self, **overrides) -> "TxnContext":
        return replace(self, **overrides)

    @classmethod
    def build(
        cls,
        *,
        funding_wallet: Wallet,
        wallets: Sequence[Wallet],
        currencies: Sequence[IssuedCurrency],
        config: dict,
        base_fee_drops: AwaitInt,
        next_sequence: AwaitSeq,
    ) -> "TxnContext":
        return cls(
            wallets=wallets,
            currencies=currencies,
            funding_wallet=funding_wallet,
            config=config,
            base_fee_drops=base_fee_drops,
            next_sequence=next_sequence,
        )


token_metadata = [
    dict(
        ticker="GOOSE",
        name="goosecoin",
        icon="https://🪿.com",  # This might not work...
        asset_class="rwa",
        asset_subclass="commodity",
        issuer_name="Mother Goose",
    ),
]


def sample_omit(seq: Sequence[T], omit: T, k: int) -> list[T]:
    return sample([x for x in seq if x != omit], k)


def deep_update(base: dict, override: dict) -> dict:
    """Recursively merge override dict into base dict."""
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            deep_update(base[k], v)
        else:
            base[k] = v
    return base


def _build_payment(ctx: TxnContext) -> dict:
    """Build a Payment transaction with random source and destination."""
    src = ctx.rand_account()
    dst = ctx.rand_account(omit=[src.address])

    use_xrp = random() < ctx.config.get("amm", {}).get("xrp_chance", 0.1)

    if use_xrp or not ctx.currencies:
        amount = str(ctx.config["transactions"]["payment"]["amount"])
    else:
        available_currencies = ctx.get_account_currencies(src)

        issuer_currencies = [c for c in ctx.currencies if c.issuer == src.address]

        sendable_currencies = list(set(available_currencies + issuer_currencies))

        if sendable_currencies:
            currency = choice(sendable_currencies)
            amount = {
                "currency": currency.currency,
                "issuer": currency.issuer,
                "value": "100",  # 100 units of the currency
            }
        else:
            amount = str(ctx.config["transactions"]["payment"]["amount"])

    result = {
        "TransactionType": "Payment",
        "Account": src.address,
        "Destination": dst.address,
        "Amount": amount,
    }

    return result


def _build_trustset(ctx: TxnContext) -> dict:
    """Build a TrustSet transaction with random account and currency.

    Picks a currency where:
      1. issuer != src.address (prevents temDST_IS_SRC)
      2. currency not in src's existing trustlines (creates useful new trustlines)
    """
    src = ctx.rand_account()

    existing_trustlines = ctx.get_account_currencies(src)
    existing_keys = {(c.currency, c.issuer) for c in existing_trustlines}

    available = [c for c in ctx.currencies if c.issuer != src.address and (c.currency, c.issuer) not in existing_keys]

    if not available:
        available = [c for c in ctx.currencies if c.issuer != src.address]
        if not available:
            raise RuntimeError(f"No currencies available for {src.address} to trust")

    cur = choice(available)

    result = {
        "TransactionType": "TrustSet",
        "Account": src.address,
        "LimitAmount": {
            "currency": cur.currency,
            "issuer": cur.issuer,
            "value": str(ctx.config["transactions"]["trustset"]["limit"]),  # From config
        },
    }

    return result


def _build_offer_create(ctx: TxnContext) -> dict:
    """Build an OfferCreate transaction to trade currencies on the DEX.

    Creates offers to exchange XRP/IOU or IOU/IOU pairs.
    """
    src = ctx.rand_account()

    use_xrp = random() < 0.5

    if use_xrp or not ctx.currencies:
        currency = ctx.rand_currency() if ctx.currencies else None
        if currency:
            if random() < 0.5:
                taker_pays = str(randrange(1_000_000, 100_000_000))  # XRP in drops
                taker_gets = {
                    "currency": currency.currency,
                    "issuer": currency.issuer,
                    "value": str(randrange(10, 1000)),
                }
            else:
                taker_pays = {
                    "currency": currency.currency,
                    "issuer": currency.issuer,
                    "value": str(randrange(10, 1000)),
                }
                taker_gets = str(randrange(1_000_000, 100_000_000))  # XRP in drops
        else:
            taker_pays = str(randrange(1_000_000, 100_000_000))
            taker_gets = str(randrange(1_000_000, 100_000_000))
    else:
        if len(ctx.currencies) >= 2:
            cur1, cur2 = sample(ctx.currencies, 2)
        else:
            cur1 = cur2 = ctx.rand_currency()

        taker_pays = {
            "currency": cur1.currency,
            "issuer": cur1.issuer,
            "value": str(randrange(10, 1000)),
        }
        taker_gets = {
            "currency": cur2.currency,
            "issuer": cur2.issuer,
            "value": str(randrange(10, 1000)),
        }

    return {
        "TransactionType": "OfferCreate",
        "Account": src.address,
        "TakerPays": taker_pays,
        "TakerGets": taker_gets,
    }


def _build_offer_cancel(ctx: TxnContext) -> dict:
    """Build an OfferCancel transaction to cancel an existing offer.

    Requires at least one IOU offer to exist in tracking.
    """
    if not ctx.offers:
        raise RuntimeError("No offers available to cancel")

    iou_offers = {k: v for k, v in ctx.offers.items() if v.get("type") == "IOUOffer"}
    if not iou_offers:
        raise RuntimeError("No IOU offers available to cancel")

    offer_id, offer_data = choice(list(iou_offers.items()))

    return {
        "TransactionType": "OfferCancel",
        "Account": offer_data["owner"],
        "OfferSequence": offer_data["sequence"],  # Sequence number when offer was created
    }


def _build_accountset(ctx: TxnContext) -> dict:
    """Build an AccountSet transaction with random account."""
    src = ctx.rand_account()
    return {
        "TransactionType": "AccountSet",
        "Account": src.address,
    }


def _build_nftoken_mint(ctx: TxnContext) -> dict:
    """Build an NFTokenMint transaction with random account."""
    src = ctx.rand_account()
    memo_msg = "Some really cool info no doubt"
    memo = Memo(memo_data=memo_msg.encode("utf-8").hex())
    return {
        "TransactionType": "NFTokenMint",
        "Account": src.address,
        "NFTokenTaxon": 0,
        "memos": [memo],
    }


def _build_nftoken_burn(ctx: TxnContext) -> dict:
    """Build an NFTokenBurn transaction to burn a random NFT.

    Requires at least one NFT to exist in tracking.
    """
    if not ctx.nfts:
        raise RuntimeError("No NFTs available to burn")

    nft_id, owner = choice(list(ctx.nfts.items()))

    return {
        "TransactionType": "NFTokenBurn",
        "Account": owner,
        "NFTokenID": nft_id,
    }


def _build_nftoken_create_offer(ctx: TxnContext) -> dict:
    """Build an NFTokenCreateOffer transaction to create a sell or buy offer.

    Randomly creates either:
    - Sell offer: owner offers to sell their NFT
    - Buy offer: non-owner offers to buy someone's NFT
    """
    is_sell_offer = random() < 0.5

    if is_sell_offer:
        if not ctx.nfts:
            raise RuntimeError("No NFTs available to create sell offer")

        nft_id, owner = choice(list(ctx.nfts.items()))

        return {
            "TransactionType": "NFTokenCreateOffer",
            "Account": owner,
            "NFTokenID": nft_id,
            "Amount": str(randrange(1_000_000, 100_000_000)),  # 1-100 XRP in drops
            "Flags": 1,  # tfSellNFToken flag
        }
    else:
        if not ctx.nfts:
            raise RuntimeError("No NFTs available to create buy offer")

        nft_id, _owner = choice(list(ctx.nfts.items()))
        buyer = ctx.rand_account()

        return {
            "TransactionType": "NFTokenCreateOffer",
            "Account": buyer.address,
            "NFTokenID": nft_id,
            "Amount": str(randrange(1_000_000, 100_000_000)),  # 1-100 XRP in drops
            "Owner": _owner,  # Owner of the NFT (required for buy offers)
        }


def _build_nftoken_cancel_offer(ctx: TxnContext) -> dict:
    """Build an NFTokenCancelOffer transaction to cancel an existing offer.

    Requires at least one NFT offer to exist in tracking.
    """
    if not ctx.offers:
        raise RuntimeError("No NFT offers available to cancel")

    nft_offers = {k: v for k, v in ctx.offers.items() if v.get("type") == "NFTokenOffer"}
    if not nft_offers:
        raise RuntimeError("No NFT offers available to cancel")

    offer_id, offer_data = choice(list(nft_offers.items()))

    return {
        "TransactionType": "NFTokenCancelOffer",
        "Account": offer_data["owner"],
        "NFTokenOffers": [offer_id],  # Can cancel multiple offers in one txn
    }


def _build_nftoken_accept_offer(ctx: TxnContext) -> dict:
    """Build an NFTokenAcceptOffer transaction to accept an existing offer.

    Requires at least one NFT offer to exist in tracking.
    """
    if not ctx.offers:
        raise RuntimeError("No NFT offers available to accept")

    nft_offers = {k: v for k, v in ctx.offers.items() if v.get("type") == "NFTokenOffer"}
    if not nft_offers:
        raise RuntimeError("No NFT offers available to accept")

    offer_id, offer_data = choice(list(nft_offers.items()))

    if offer_data.get("is_sell_offer"):
        acceptor = ctx.rand_account()
        return {
            "TransactionType": "NFTokenAcceptOffer",
            "Account": acceptor.address,
            "NFTokenSellOffer": offer_id,
        }
    else:
        nft_id = offer_data.get("nft_id")
        if nft_id and nft_id in (ctx.nfts or {}):
            owner = ctx.nfts[nft_id]
            return {
                "TransactionType": "NFTokenAcceptOffer",
                "Account": owner,
                "NFTokenBuyOffer": offer_id,
            }
        else:
            acceptor = ctx.rand_account()
            return {
                "TransactionType": "NFTokenAcceptOffer",
                "Account": acceptor.address,
                "NFTokenBuyOffer": offer_id,
            }


def _build_ticket_create(ctx: TxnContext) -> dict:
    """Build a TicketCreate transaction to create tickets for an account.

    Tickets allow transactions to be submitted out of sequence order.
    """
    src = ctx.rand_account()

    ticket_count = randrange(1, 11)

    return {
        "TransactionType": "TicketCreate",
        "Account": src.address,
        "TicketCount": ticket_count,
    }


def _build_mptoken_issuance_create(ctx: TxnContext) -> dict:
    """Build an MPTokenIssuanceCreate transaction with random account."""
    src = ctx.rand_account()
    metadata_hex = json.dumps(choice(token_metadata)).encode("utf-8").hex()
    return {
        "TransactionType": "MPTokenIssuanceCreate",
        "Account": src.address,
        "MPTokenMetadata": metadata_hex,
    }


def _build_mptoken_issuance_set(ctx: TxnContext) -> dict:
    """Build an MPTokenIssuanceSet transaction to modify MPToken properties."""
    src = ctx.rand_account()
    mpt_id = ctx.rand_mptoken_id()

    return {
        "TransactionType": "MPTokenIssuanceSet",
        "Account": src.address,
        "MPTokenIssuanceID": mpt_id,
    }


def _build_mptoken_authorize(ctx: TxnContext) -> dict:
    """Build an MPTokenAuthorize transaction to authorize/unauthorize holder."""
    src = ctx.rand_account()
    mpt_id = ctx.rand_mptoken_id()

    return {
        "TransactionType": "MPTokenAuthorize",
        "Account": src.address,
        "MPTokenIssuanceID": mpt_id,
    }


def _build_mptoken_issuance_destroy(ctx: TxnContext) -> dict:
    """Build an MPTokenIssuanceDestroy transaction to destroy an MPToken issuance."""
    src = ctx.rand_account()
    mpt_id = ctx.rand_mptoken_id()

    return {
        "TransactionType": "MPTokenIssuanceDestroy",
        "Account": src.address,
        "MPTokenIssuanceID": mpt_id,
    }


async def _build_batch(ctx: TxnContext) -> dict:
    """Build a Batch transaction with random inner transactions of various types."""
    src = ctx.rand_account()

    num_inner = randrange(2, 9)

    batch_seq = await ctx.next_sequence(src.address)
    inner_sequences = [await ctx.next_sequence(src.address) for _ in range(num_inner)]

    inner_txns = []
    for seq in inner_sequences:
        txn_type = choice(["Payment", "TrustSet", "AccountSet", "NFTokenMint"])

        if txn_type == "Payment":
            use_xrp = random() < 0.5
            if use_xrp or not ctx.currencies:
                amount = str(randrange(1_000_000, 100_000_000))  # 1-100 XRP in drops
            else:
                currency = ctx.rand_currency()
                amount = IssuedCurrencyAmount(
                    currency=currency.currency,
                    issuer=currency.issuer,
                    value=str(randrange(10, 1000)),
                )

            inner_tx = Payment(
                account=src.address,
                destination=choice_omit(ctx.wallets, [src]).address,
                amount=amount,
                fee="0",
                signing_pub_key="",
                flags=TransactionFlag.TF_INNER_BATCH_TXN,
                sequence=seq,
            )

        elif txn_type == "TrustSet":
            available_cur = [c for c in ctx.currencies if c.issuer != src.address]
            if not available_cur:
                inner_tx = AccountSet(
                    account=src.address,
                    fee="0",
                    signing_pub_key="",
                    flags=TransactionFlag.TF_INNER_BATCH_TXN,
                    sequence=seq,
                )
            else:
                cur = choice(available_cur)
                inner_tx = TrustSet(
                    account=src.address,
                    limit_amount=IssuedCurrencyAmount(
                        currency=cur.currency,
                        issuer=cur.issuer,
                        value=str(ctx.config["transactions"]["trustset"]["limit"]),
                    ),
                    fee="0",
                    signing_pub_key="",
                    flags=TransactionFlag.TF_INNER_BATCH_TXN,
                    sequence=seq,
                )

        elif txn_type == "AccountSet":
            inner_tx = AccountSet(
                account=src.address,
                fee="0",
                signing_pub_key="",
                flags=TransactionFlag.TF_INNER_BATCH_TXN,
                sequence=seq,
            )

        elif txn_type == "NFTokenMint":
            memo = Memo(memo_data="Batch NFT".encode("utf-8").hex())
            inner_tx = NFTokenMint(
                account=src.address,
                nftoken_taxon=0,
                fee="0",
                signing_pub_key="",
                flags=TransactionFlag.TF_INNER_BATCH_TXN,
                sequence=seq,
                memos=[memo],
            )

        inner_txns.append({"RawTransaction": inner_tx})

    batch_mode = choice(
        [
            BatchFlag.TF_ALL_OR_NOTHING,
            BatchFlag.TF_ONLY_ONE,
            BatchFlag.TF_UNTIL_FAILURE,
            BatchFlag.TF_INDEPENDENT,
        ]
    )

    return {
        "TransactionType": "Batch",
        "Account": src.address,
        "Sequence": batch_seq,  # Explicitly set so build_sign_and_track won't allocate a new one
        "Flags": batch_mode,
        "RawTransactions": inner_txns,
    }


def _build_amm_create(ctx: TxnContext) -> dict:
    """Build an AMMCreate transaction with random currency pair.

    NOTE: Fee will be set to owner_reserve in build_sign_and_track based on TransactionType.
    """
    src = ctx.rand_account()

    max_attempts = 10
    amount_xrp = "1000000000"  # 1000 XRP (in drops)

    for attempt in range(max_attempts):
        currency = ctx.rand_currency()
        amount_iou = {
            "currency": currency.currency,
            "issuer": currency.issuer,
            "value": str(ctx.config["amm"]["default_amm_token_deposit"]),
        }

        if not ctx.amm_pool_exists(amount_xrp, amount_iou):
            return {
                "TransactionType": "AMMCreate",
                "Account": src.address,
                "Amount": amount_xrp,
                "Amount2": amount_iou,
                "TradingFee": ctx.config["amm"]["trading_fee"],  # From config
            }

    return {
        "TransactionType": "AMMCreate",
        "Account": src.address,
        "Amount": amount_xrp,
        "Amount2": amount_iou,
        "TradingFee": ctx.config["amm"]["trading_fee"],
    }


def _build_amm_deposit(ctx: TxnContext) -> dict | None:
    """Build an AMMDeposit transaction to add liquidity to an existing AMM pool.

    Uses TF_TWO_ASSET flag for dual-asset deposit.
    Only picks pools where src account has the required assets.
    """
    src = ctx.rand_account()
    account_currencies = {(c.currency, c.issuer) for c in ctx.get_account_currencies(src)}
    has_balance_data = bool(account_currencies)

    eligible_pools = []
    for p in ctx.amm_pool_registry or []:
        a1, a2 = p["asset1"], p["asset2"]
        if a1.get("currency") == "XRP":
            # XRP/IOU pool: account always has XRP; if we have balance data, verify IOU too
            if not has_balance_data or (a2["currency"], a2["issuer"]) in account_currencies:
                eligible_pools.append(p)
        else:
            # IOU/IOU pool: only proceed if we can confirm both assets
            if (
                has_balance_data
                and (a1["currency"], a1["issuer"]) in account_currencies
                and (a2["currency"], a2["issuer"]) in account_currencies
            ):
                eligible_pools.append(p)

    if not eligible_pools:
        return None
    pool = choice(eligible_pools)

    asset1 = pool["asset1"]
    asset2 = pool["asset2"]

    amm_cfg = ctx.config.get("amm", {})

    if asset1.get("currency") == "XRP":
        amount1 = amm_cfg.get("deposit_amount_xrp", "1000000000")
        amount2 = {
            "currency": asset2["currency"],
            "issuer": asset2["issuer"],
            "value": amm_cfg.get("deposit_amount_iou", "500"),
        }
        asset1_field = {"currency": "XRP"}
        asset2_field = {"currency": asset2["currency"], "issuer": asset2["issuer"]}
    else:
        amount1 = {
            "currency": asset1["currency"],
            "issuer": asset1["issuer"],
            "value": amm_cfg.get("deposit_amount_iou", "500"),
        }
        amount2 = {
            "currency": asset2["currency"],
            "issuer": asset2["issuer"],
            "value": amm_cfg.get("deposit_amount_iou", "500"),
        }
        asset1_field = {"currency": asset1["currency"], "issuer": asset1["issuer"]}
        asset2_field = {"currency": asset2["currency"], "issuer": asset2["issuer"]}

    return {
        "TransactionType": "AMMDeposit",
        "Account": src.address,
        "Asset": asset1_field,
        "Asset2": asset2_field,
        "Amount": amount1,
        "Amount2": amount2,
        "Flags": AMMDepositFlag.TF_TWO_ASSET,
    }


def _build_amm_withdraw(ctx: TxnContext) -> dict | None:
    """Build an AMMWithdraw transaction to remove liquidity from an existing AMM pool.

    Uses TF_TWO_ASSET flag for proportional dual-asset withdrawal.
    Withdraws 10% of deposit amounts to keep pools healthy.
    Only picks pools where src holds LP tokens — returns None if no eligible pool found.
    """
    lp_holders: set[str] = set()
    for p in (ctx.amm_pool_registry or []):
        for addr in p.get("lp_holders", [p.get("creator", "")]):
            lp_holders.add(addr)
    if not lp_holders:
        return None
    src = ctx.rand_owner(lp_holders)
    if src is None:
        return None
    eligible_pools = [
        p for p in (ctx.amm_pool_registry or []) if src.address in p.get("lp_holders", [p.get("creator", "")])
    ]
    if not eligible_pools:
        return None
    pool = choice(eligible_pools)

    asset1 = pool["asset1"]
    asset2 = pool["asset2"]

    amm_cfg = ctx.config.get("amm", {})
    withdraw_xrp = str(int(int(amm_cfg.get("deposit_amount_xrp", "1000000000")) * 0.1))
    withdraw_iou = str(float(amm_cfg.get("deposit_amount_iou", "500")) * 0.1)

    if asset1.get("currency") == "XRP":
        amount1 = withdraw_xrp
        amount2 = {
            "currency": asset2["currency"],
            "issuer": asset2["issuer"],
            "value": withdraw_iou,
        }
        asset1_field = {"currency": "XRP"}
        asset2_field = {"currency": asset2["currency"], "issuer": asset2["issuer"]}
    else:
        amount1 = {
            "currency": asset1["currency"],
            "issuer": asset1["issuer"],
            "value": withdraw_iou,
        }
        amount2 = {
            "currency": asset2["currency"],
            "issuer": asset2["issuer"],
            "value": withdraw_iou,
        }
        asset1_field = {"currency": asset1["currency"], "issuer": asset1["issuer"]}
        asset2_field = {"currency": asset2["currency"], "issuer": asset2["issuer"]}

    return {
        "TransactionType": "AMMWithdraw",
        "Account": src.address,
        "Asset": asset1_field,
        "Asset2": asset2_field,
        "Amount": amount1,
        "Amount2": amount2,
        "Flags": AMMWithdrawFlag.TF_TWO_ASSET,
    }


# ---------------------------------------------------------------------------
# Helpers for new transaction families
# ---------------------------------------------------------------------------


def _random_hex(n: int) -> str:
    """Generate a random hex string of *n* bytes."""
    return bytes(randrange(256) for _ in range(n)).hex()


def _random_credential_type(cfg: dict) -> str:
    """Random hex-encoded credential type, length from config."""
    max_bytes = cfg.get("transactions", {}).get("credential_create", {}).get("credential_type_max_bytes", 64)
    return _random_hex(randrange(1, max_bytes + 1))


def _random_credential_uri(cfg: dict) -> str:
    """Random hex-encoded URI for credentials, length from config."""
    max_bytes = cfg.get("transactions", {}).get("credential_create", {}).get("uri_max_bytes", 256)
    return _random_hex(randrange(10, max_bytes + 1))


def _random_vault_asset(ctx: TxnContext) -> dict:
    """Pick a random asset suitable for vault creation: IOU, MPT, or XRP."""
    roll = random()
    if ctx.currencies and roll < 0.5:
        cur = ctx.rand_currency()
        return {"currency": cur.currency, "issuer": cur.issuer}
    if ctx.mptoken_issuance_ids and roll < 0.8:
        return {"mpt_issuance_id": ctx.rand_mptoken_id()}
    if ctx.currencies:
        cur = ctx.rand_currency()
        return {"currency": cur.currency, "issuer": cur.issuer}
    return {"currency": "XRP"}


def _vault_amount_for_asset(asset: dict, cfg: dict) -> str | dict:
    """Create an Amount matching a vault's asset type, ranges from config."""
    vcfg = cfg.get("transactions", {}).get("vault_deposit", {})
    if "mpt_issuance_id" in asset:
        lo = vcfg.get("mpt_amount_min", 1)
        hi = vcfg.get("mpt_amount_max", 10_000)
        return {"mpt_issuance_id": asset["mpt_issuance_id"], "value": str(randrange(lo, hi + 1))}
    if asset.get("currency") and asset.get("issuer"):
        lo = vcfg.get("iou_amount_min", 1)
        hi = vcfg.get("iou_amount_max", 10_000)
        return {"currency": asset["currency"], "issuer": asset["issuer"], "value": str(randrange(lo, hi + 1))}
    # XRP — drops
    lo = vcfg.get("xrp_drops_min", 1_000_000)
    hi = vcfg.get("xrp_drops_max", 100_000_000)
    return str(randrange(lo, hi + 1))


# ---------------------------------------------------------------------------
# Delegation
# ---------------------------------------------------------------------------
def _build_delegate_set(ctx: TxnContext) -> dict:
    """Build a DelegateSet transaction to delegate permissions to another account.

    Uses GranularPermission enum values from xrpl-py, matching upstream branch.
    Picks 1-3 random permissions from the 12 available granular permissions.
    """
    src, delegate = ctx.rand_accounts(2)
    all_perms = list(GranularPermission)
    max_p = ctx.config.get("transactions", {}).get("delegate_set", {}).get("max_permissions", 3)
    num_perms = min(len(all_perms), randrange(1, max_p + 1))
    selected = sample(all_perms, num_perms)
    permissions = [{"Permission": {"PermissionValue": p.value}} for p in selected]
    return {
        "TransactionType": "DelegateSet",
        "Account": src.address,
        "Authorize": delegate.address,
        "Permissions": permissions,
    }


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------
def _build_credential_create(ctx: TxnContext) -> dict:
    """Build a CredentialCreate — issuer attests about a subject.

    Includes Expiration (1 hour – 30 days from now) and URI, matching upstream params.py ranges.
    """
    import time

    issuer = ctx.rand_account()
    subject = ctx.rand_account(omit=[issuer.address])
    ccfg = ctx.config.get("transactions", {}).get("credential_create", {})
    # Expiration: Ripple epoch = Unix epoch - 946684800
    ripple_epoch_offset = 946684800
    exp_min = ccfg.get("expiration_min_offset", 3600)
    exp_max = ccfg.get("expiration_max_offset", 2592000)
    expiration = int(time.time()) - ripple_epoch_offset + randrange(exp_min, exp_max + 1)
    return {
        "TransactionType": "CredentialCreate",
        "Account": issuer.address,
        "Subject": subject.address,
        "CredentialType": _random_credential_type(ctx.config),
        "Expiration": expiration,
        "URI": _random_credential_uri(ctx.config),
    }


def _build_credential_accept(ctx: TxnContext) -> dict | None:
    """Build a CredentialAccept — subject accepts an issued credential."""
    if not ctx.credentials:
        return None
    unaccepted = [c for c in ctx.credentials if not c.get("accepted")]
    if not unaccepted:
        return None
    subjects = {c["subject"] for c in unaccepted}
    src = ctx.rand_owner(subjects)
    if src is None:
        return None
    eligible = [c for c in unaccepted if c["subject"] == src.address]
    if not eligible:
        return None
    cred = choice(eligible)
    return {
        "TransactionType": "CredentialAccept",
        "Account": src.address,
        "Issuer": cred["issuer"],
        "CredentialType": cred["credential_type"],
    }


def _build_credential_delete(ctx: TxnContext) -> dict | None:
    """Build a CredentialDelete — issuer or subject removes a credential."""
    if not ctx.credentials:
        return None
    participants = {c["issuer"] for c in ctx.credentials} | {c["subject"] for c in ctx.credentials}
    src = ctx.rand_owner(participants)
    if src is None:
        return None
    eligible = [c for c in ctx.credentials if c["issuer"] == src.address or c["subject"] == src.address]
    if not eligible:
        return None
    cred = choice(eligible)
    return {
        "TransactionType": "CredentialDelete",
        "Account": src.address,
        "Subject": cred["subject"],
        "Issuer": cred["issuer"],
        "CredentialType": cred["credential_type"],
    }


# ---------------------------------------------------------------------------
# Permissioned Domains
# ---------------------------------------------------------------------------
def _build_permissioned_domain_set(ctx: TxnContext) -> dict:
    """Build a PermissionedDomainSet — create or update a permissioned domain.

    Accepts 1-10 credential definitions (matching upstream params.domain_credential_count).
    Each credential references a random account as issuer with a random credential type.
    """
    src = ctx.rand_account()
    dcfg = ctx.config.get("transactions", {}).get("permissioned_domain_set", {})
    max_creds = dcfg.get("max_credentials", 10)
    num_creds = randrange(1, max_creds + 1)
    accepted = [
        {
            "Credential": {
                "Issuer": ctx.rand_account().address,
                "CredentialType": _random_credential_type(ctx.config),
            }
        }
        for _ in range(num_creds)
    ]
    result = {
        "TransactionType": "PermissionedDomainSet",
        "Account": src.address,
        "AcceptedCredentials": accepted,
    }
    # Optionally update an existing domain owned by src
    if ctx.domains and random() < 0.3:
        owned = [d for d in ctx.domains if d["owner"] == src.address]
        if owned:
            result["DomainID"] = choice(owned)["domain_id"]
    return result


def _build_permissioned_domain_delete(ctx: TxnContext) -> dict | None:
    """Build a PermissionedDomainDelete — owner removes a domain."""
    if not ctx.domains:
        return None
    owners = {d["owner"] for d in ctx.domains}
    src = ctx.rand_owner(owners)
    if src is None:
        return None
    owned = [d for d in ctx.domains if d["owner"] == src.address]
    if not owned:
        return None
    domain = choice(owned)
    return {
        "TransactionType": "PermissionedDomainDelete",
        "Account": src.address,
        "DomainID": domain["domain_id"],
    }


# ---------------------------------------------------------------------------
# Vaults
# ---------------------------------------------------------------------------
def _build_vault_create(ctx: TxnContext) -> dict:
    """Build a VaultCreate — open a new vault for an asset.

    Includes AssetsMaximum (100M-10B drops) and Data (1-256 bytes hex),
    matching upstream params.py ranges.
    """
    src = ctx.rand_account()
    asset = _random_vault_asset(ctx)
    vcfg = ctx.config.get("transactions", {}).get("vault_create", {})
    am_min = vcfg.get("assets_maximum_min", 100_000_000)
    am_max = vcfg.get("assets_maximum_max", 10_000_000_000)
    data_max = vcfg.get("data_max_bytes", 256)
    return {
        "TransactionType": "VaultCreate",
        "Account": src.address,
        "Asset": asset,
        "AssetsMaximum": str(randrange(am_min, am_max + 1)),
        "Data": _random_hex(randrange(1, data_max + 1)),
    }


def _build_vault_set(ctx: TxnContext) -> dict | None:
    """Build a VaultSet — owner updates vault settings.

    Includes AssetsMaximum and Data, matching upstream params.py ranges.
    """
    if not ctx.vaults:
        return None
    vault_owners = {v["owner"] for v in ctx.vaults}
    src = ctx.rand_owner(vault_owners)
    if src is None:
        return None
    owned = [v for v in ctx.vaults if v["owner"] == src.address]
    if not owned:
        return None
    vault = choice(owned)
    vcfg = ctx.config.get("transactions", {}).get("vault_create", {})
    am_min = vcfg.get("assets_maximum_min", 100_000_000)
    am_max = vcfg.get("assets_maximum_max", 10_000_000_000)
    data_max = vcfg.get("data_max_bytes", 256)
    return {
        "TransactionType": "VaultSet",
        "Account": src.address,
        "VaultID": vault["vault_id"],
        "AssetsMaximum": str(randrange(am_min, am_max + 1)),
        "Data": _random_hex(randrange(1, data_max + 1)),
    }


def _build_vault_delete(ctx: TxnContext) -> dict | None:
    """Build a VaultDelete — owner removes a vault."""
    if not ctx.vaults:
        return None
    vault_owners = {v["owner"] for v in ctx.vaults}
    src = ctx.rand_owner(vault_owners)
    if src is None:
        return None
    owned = [v for v in ctx.vaults if v["owner"] == src.address]
    if not owned:
        return None
    vault = choice(owned)
    return {
        "TransactionType": "VaultDelete",
        "Account": src.address,
        "VaultID": vault["vault_id"],
    }


def _build_vault_deposit(ctx: TxnContext) -> dict | None:
    """Build a VaultDeposit — deposit assets into an existing vault.

    Amount matches vault's asset type (IOU 1-10k, MPT 1-10k, XRP 1M-100M drops).
    """
    if not ctx.vaults:
        return None
    src = ctx.rand_account()
    vault = choice(ctx.vaults)
    return {
        "TransactionType": "VaultDeposit",
        "Account": src.address,
        "VaultID": vault["vault_id"],
        "Amount": _vault_amount_for_asset(vault.get("asset", {}), ctx.config),
    }


def _build_vault_withdraw(ctx: TxnContext) -> dict | None:
    """Build a VaultWithdraw — owner withdraws from a vault.

    Amount matches vault's asset type, same ranges as deposit.
    """
    if not ctx.vaults:
        return None
    vault_owners = {v["owner"] for v in ctx.vaults}
    src = ctx.rand_owner(vault_owners)
    if src is None:
        return None
    owned = [v for v in ctx.vaults if v["owner"] == src.address]
    if not owned:
        return None
    vault = choice(owned)
    return {
        "TransactionType": "VaultWithdraw",
        "Account": src.address,
        "VaultID": vault["vault_id"],
        "Amount": _vault_amount_for_asset(vault.get("asset", {}), ctx.config),
    }


def _build_vault_clawback(ctx: TxnContext) -> dict | None:
    """Build a VaultClawback — vault owner claws back from a holder.

    Matches upstream branch: Account = vault owner, Holder = random other account.
    No Amount field (claws back all).
    """
    if not ctx.vaults:
        return None
    vault_owners = {v["owner"] for v in ctx.vaults}
    src = ctx.rand_owner(vault_owners)
    if src is None:
        return None
    owned = [v for v in ctx.vaults if v["owner"] == src.address]
    if not owned:
        return None
    vault = choice(owned)
    holder = ctx.rand_account(omit=[src.address])
    return {
        "TransactionType": "VaultClawback",
        "Account": src.address,
        "VaultID": vault["vault_id"],
        "Holder": holder.address,
    }


_BUILDERS: dict[str, tuple[Callable[[TxnContext], dict], type[Transaction]]] = {
    "Payment": (_build_payment, Payment),
    "TrustSet": (_build_trustset, TrustSet),
    "OfferCreate": (_build_offer_create, OfferCreate),
    "OfferCancel": (_build_offer_cancel, OfferCancel),
    "AccountSet": (_build_accountset, AccountSet),
    "NFTokenMint": (_build_nftoken_mint, NFTokenMint),
    "NFTokenBurn": (_build_nftoken_burn, NFTokenBurn),
    "NFTokenCreateOffer": (_build_nftoken_create_offer, NFTokenCreateOffer),
    "NFTokenCancelOffer": (_build_nftoken_cancel_offer, NFTokenCancelOffer),
    "NFTokenAcceptOffer": (_build_nftoken_accept_offer, NFTokenAcceptOffer),
    "TicketCreate": (_build_ticket_create, TicketCreate),
    "MPTokenIssuanceCreate": (_build_mptoken_issuance_create, MPTokenIssuanceCreate),
    "MPTokenIssuanceSet": (_build_mptoken_issuance_set, MPTokenIssuanceSet),
    "MPTokenAuthorize": (_build_mptoken_authorize, MPTokenAuthorize),
    "MPTokenIssuanceDestroy": (_build_mptoken_issuance_destroy, MPTokenIssuanceDestroy),
    "AMMCreate": (_build_amm_create, AMMCreate),
    "AMMDeposit": (_build_amm_deposit, AMMDeposit),
    "AMMWithdraw": (_build_amm_withdraw, AMMWithdraw),
    "Batch": (_build_batch, Batch),
    # Delegation
    "DelegateSet": (_build_delegate_set, DelegateSet),
    # Credentials
    "CredentialCreate": (_build_credential_create, CredentialCreate),
    "CredentialAccept": (_build_credential_accept, CredentialAccept),
    "CredentialDelete": (_build_credential_delete, CredentialDelete),
    # Permissioned Domains
    "PermissionedDomainSet": (_build_permissioned_domain_set, PermissionedDomainSet),
    "PermissionedDomainDelete": (_build_permissioned_domain_delete, PermissionedDomainDelete),
    # Vaults
    "VaultCreate": (_build_vault_create, VaultCreate),
    "VaultSet": (_build_vault_set, VaultSet),
    "VaultDelete": (_build_vault_delete, VaultDelete),
    "VaultDeposit": (_build_vault_deposit, VaultDeposit),
    "VaultWithdraw": (_build_vault_withdraw, VaultWithdraw),
    "VaultClawback": (_build_vault_clawback, VaultClawback),
}


def pick_eligible_txn_type(wallet: "Wallet", ctx: TxnContext) -> str | None:
    """Return a weight-sampled eligible transaction type for wallet, or None if none available.

    Applies global capability filters (disabled, MPT IDs, NFTs, offers, AMM pools)
    and per-account filters (LP holder for AMMWithdraw, asset availability for AMMDeposit,
    offer ownership for OfferCancel). Excludes Batch (async builder, multi-seq allocation).
    """
    candidates = list(_BUILDERS.keys())

    disabled = (
        ctx.disabled_types
        if ctx.disabled_types is not None
        else set(ctx.config.get("transactions", {}).get("disabled", []))
    )
    if disabled:
        candidates = [t for t in candidates if t not in disabled]

    if not ctx.mptoken_issuance_ids:
        mpt_ops = {"MPTokenAuthorize", "MPTokenIssuanceSet", "MPTokenIssuanceDestroy"}
        candidates = [t for t in candidates if t not in mpt_ops]

    if not ctx.nfts:
        candidates = [t for t in candidates if t not in {"NFTokenBurn", "NFTokenCreateOffer"}]

    has_nft_offers = ctx.offers and any(v.get("type") == "NFTokenOffer" for v in ctx.offers.values())
    if not has_nft_offers:
        candidates = [t for t in candidates if t not in {"NFTokenCancelOffer", "NFTokenAcceptOffer"}]

    if not ctx.amm_pool_registry:
        candidates = [t for t in candidates if t not in {"AMMDeposit", "AMMWithdraw"}]

    # Global: Credentials — Accept/Delete need existing credentials
    if not ctx.credentials:
        candidates = [t for t in candidates if t not in {"CredentialAccept", "CredentialDelete"}]

    # Global: Domains — Delete needs existing domains
    if not ctx.domains:
        candidates = [t for t in candidates if t != "PermissionedDomainDelete"]

    # Global: Vaults — operations on existing vaults need at least one
    if not ctx.vaults:
        vault_ops = {"VaultSet", "VaultDelete", "VaultDeposit", "VaultWithdraw", "VaultClawback"}
        candidates = [t for t in candidates if t not in vault_ops]

    # Per-account: OfferCancel requires wallet to own at least one IOU offer
    if "OfferCancel" in candidates:
        has_own_offer = ctx.offers and any(
            v.get("type") == "IOUOffer" and v.get("owner") == wallet.address for v in ctx.offers.values()
        )
        if not has_own_offer:
            candidates = [t for t in candidates if t != "OfferCancel"]

    # Per-account: AMMWithdraw requires wallet to hold LP tokens in at least one pool
    if "AMMWithdraw" in candidates:
        is_lp = any(
            wallet.address in p.get("lp_holders", [p.get("creator", "")]) for p in (ctx.amm_pool_registry or [])
        )
        if not is_lp:
            candidates = [t for t in candidates if t != "AMMWithdraw"]

    # Per-account: AMMDeposit requires at least one pool where wallet can provide both assets
    if "AMMDeposit" in candidates:
        account_currencies = {(c.currency, c.issuer) for c in ctx.get_account_currencies(wallet)}
        has_balance_data = bool(account_currencies)
        eligible = any(
            (
                p["asset1"].get("currency") == "XRP"
                and (not has_balance_data or (p["asset2"].get("currency"), p["asset2"].get("issuer")) in account_currencies)
            )
            or (
                has_balance_data
                and "issuer" in p["asset1"] and "issuer" in p["asset2"]
                and (p["asset1"]["currency"], p["asset1"]["issuer"]) in account_currencies
                and (p["asset2"]["currency"], p["asset2"]["issuer"]) in account_currencies
            )
            for p in (ctx.amm_pool_registry or [])
        )
        if not eligible:
            candidates = [t for t in candidates if t != "AMMDeposit"]

    # Per-account: CredentialAccept needs an unaccepted credential where wallet is subject
    if "CredentialAccept" in candidates:
        has_unaccepted = ctx.credentials and any(
            c["subject"] == wallet.address and not c.get("accepted") for c in ctx.credentials
        )
        if not has_unaccepted:
            candidates = [t for t in candidates if t != "CredentialAccept"]

    # Per-account: CredentialDelete needs a credential where wallet is issuer or subject
    if "CredentialDelete" in candidates:
        involved = ctx.credentials and any(
            c["issuer"] == wallet.address or c["subject"] == wallet.address for c in ctx.credentials
        )
        if not involved:
            candidates = [t for t in candidates if t != "CredentialDelete"]

    # Per-account: PermissionedDomainDelete needs wallet to own a domain
    if "PermissionedDomainDelete" in candidates:
        owns_domain = ctx.domains and any(d["owner"] == wallet.address for d in ctx.domains)
        if not owns_domain:
            candidates = [t for t in candidates if t != "PermissionedDomainDelete"]

    # Per-account: VaultSet/Delete/Withdraw/Clawback need wallet to own a vault
    if any(t in candidates for t in ("VaultSet", "VaultDelete", "VaultWithdraw", "VaultClawback")):
        owns_vault = ctx.vaults and any(v["owner"] == wallet.address for v in ctx.vaults)
        if not owns_vault:
            candidates = [t for t in candidates if t not in {"VaultSet", "VaultDelete", "VaultWithdraw", "VaultClawback"}]

    # Batch uses an async builder with multi-seq allocations — not supported in sync pre-build path
    candidates = [t for t in candidates if t != "Batch"]

    if not candidates:
        return None

    percentages = ctx.config.get("transactions", {}).get("percentages", {})
    defined_total = sum(percentages.get(t, 0) for t in candidates)
    remaining = 1.0 - defined_total
    undefined_types = [t for t in candidates if t not in percentages]
    per_undefined = remaining / len(undefined_types) if undefined_types else 0
    weights = [percentages.get(t, per_undefined) for t in candidates]
    return choices(candidates, weights=weights, k=1)[0]


def build_txn_dict(txn_type: str, ctx: TxnContext) -> dict | None:
    """Call the synchronous builder for txn_type and return the raw dict (or None if ineligible)."""
    builder_fn, _ = _BUILDERS[txn_type]
    return builder_fn(ctx)


def txn_model_cls(txn_type: str) -> type[Transaction]:
    """Return the xrpl-py model class for txn_type."""
    _, model_cls = _BUILDERS[txn_type]
    return model_cls


async def generate_txn(ctx: TxnContext, txn_type: str | None = None, **overrides: Any) -> Transaction:
    """Generate a transaction with sane defaults.

    Args:
        ctx: Transaction context with wallets, currencies, and defaults
        txn_type: Transaction type name (e.g., "Payment", "TrustSet").
                 If None, picks a random available type.
        **overrides: Additional fields to override in the transaction

    Returns:
        A fully formed Transaction model ready to sign and submit

    Raises:
        ValueError: If txn_type is not supported
    """
    import inspect

    if txn_type is None:
        configured_types = list(_BUILDERS.keys())

        disabled_types = (
            ctx.disabled_types
            if ctx.disabled_types is not None
            else set(ctx.config.get("transactions", {}).get("disabled", []))
        )
        if disabled_types:
            configured_types = [t for t in configured_types if t not in disabled_types]
            log.debug("Disabled transaction types: %s", disabled_types)

        requires_mpt_id = {"MPTokenAuthorize", "MPTokenIssuanceSet", "MPTokenIssuanceDestroy"}

        if not ctx.mptoken_issuance_ids:
            configured_types = [t for t in configured_types if t not in requires_mpt_id]
            log.debug("No MPToken IDs available, excluding: %s", requires_mpt_id)

        requires_nfts = {"NFTokenBurn", "NFTokenCreateOffer"}

        if not ctx.nfts:
            configured_types = [t for t in configured_types if t not in requires_nfts]
            log.debug("No NFTs available, excluding: %s", requires_nfts)

        requires_nft_offers = {"NFTokenCancelOffer", "NFTokenAcceptOffer"}

        if not ctx.offers or not any(v.get("type") == "NFTokenOffer" for v in (ctx.offers or {}).values()):
            configured_types = [t for t in configured_types if t not in requires_nft_offers]
            log.debug("No NFT offers available, excluding: %s", requires_nft_offers)

        requires_iou_offers = {"OfferCancel"}

        if not ctx.offers or not any(v.get("type") == "IOUOffer" for v in (ctx.offers or {}).values()):
            configured_types = [t for t in configured_types if t not in requires_iou_offers]
            log.debug("No IOU offers available, excluding: %s", requires_iou_offers)

        requires_amm_pools = {"AMMDeposit", "AMMWithdraw"}

        if not ctx.amm_pool_registry:
            configured_types = [t for t in configured_types if t not in requires_amm_pools]
            log.debug("No AMM pools available, excluding: %s", requires_amm_pools)

        if not ctx.credentials:
            configured_types = [t for t in configured_types if t not in {"CredentialAccept", "CredentialDelete"}]

        if not ctx.domains:
            configured_types = [t for t in configured_types if t != "PermissionedDomainDelete"]

        if not ctx.vaults:
            vault_ops = {"VaultSet", "VaultDelete", "VaultDeposit", "VaultWithdraw", "VaultClawback"}
            configured_types = [t for t in configured_types if t not in vault_ops]

        if not configured_types:
            raise RuntimeError("No transaction types available to generate")

        percentages = ctx.config.get("transactions", {}).get("percentages", {})

        defined_total = sum(percentages.get(t, 0) for t in configured_types)
        remaining = 1.0 - defined_total
        undefined_types = [t for t in configured_types if t not in percentages]
        per_undefined = remaining / len(undefined_types) if undefined_types else 0

        weights = [percentages.get(t, per_undefined) for t in configured_types]

        txn_type = choices(configured_types, weights=weights, k=1)[0]
    else:
        if txn_type not in _BUILDERS:
            for builder_type in _BUILDERS.keys():
                if builder_type.lower() == str(txn_type).lower():
                    txn_type = builder_type
                    break

    log.debug("Generating %s txn", txn_type)

    builder_spec = _BUILDERS.get(txn_type)
    if not builder_spec:
        raise ValueError(f"Unsupported txn_type: {txn_type}")

    builder_fn, model_cls = builder_spec

    if inspect.iscoroutinefunction(builder_fn):
        composed = await builder_fn(ctx)
    else:
        composed = builder_fn(ctx)

    if composed is None:
        log.debug("Builder for %s returned None (account ineligible) — skipping", txn_type)
        return None

    if overrides:
        deep_update(composed, transaction_json_to_binary_codec_form(overrides))

    log.debug(f"Transaction dict for {txn_type}: {composed}")

    log.debug(f"Created {txn_type}")
    return model_cls.from_xrpl(composed)


async def create_payment(ctx: TxnContext, **overrides: Any) -> Payment:
    """Create a Payment transaction with sane defaults."""
    return await generate_txn(ctx, "Payment", **overrides)


async def create_xrp_payment(ctx: TxnContext, **overrides: Any) -> Payment:
    """Create an XRP-only Payment transaction.

    Simple, predictable, base-fee transaction for workload testing.
    Forces XRP amount regardless of xrp_chance config.
    """
    wl = list(ctx.wallets)
    if len(wl) >= 2:
        src, dst = sample(wl, 2)
    else:
        src = wl[0] if wl else ctx.funding_wallet
        dst = ctx.funding_wallet if ctx.funding_wallet is not src else src

    amount = str(ctx.config["transactions"]["payment"]["amount"])

    return await generate_txn(
        ctx,
        "Payment",
        Account=src.address,
        Destination=dst.address,
        Amount=amount,
        **overrides,
    )


async def create_trustset(ctx: TxnContext, **overrides: Any) -> TrustSet:
    """Create a TrustSet transaction with sane defaults."""
    return await generate_txn(ctx, "TrustSet", **overrides)


async def create_accountset(ctx: TxnContext, **overrides: Any) -> AccountSet:
    """Create an AccountSet transaction with sane defaults."""
    return await generate_txn(ctx, "AccountSet", **overrides)


async def create_nftoken_mint(ctx: TxnContext, **overrides: Any) -> NFTokenMint:
    """Create an NFTokenMint transaction with sane defaults."""
    return await generate_txn(ctx, "NFTokenMint", **overrides)


async def create_mptoken_issuance_create(ctx: TxnContext, **overrides: Any) -> MPTokenIssuanceCreate:
    """Create an MPTokenIssuanceCreate transaction with sane defaults."""
    return await generate_txn(ctx, "MPTokenIssuanceCreate", **overrides)


async def create_batch(ctx: TxnContext, **overrides: Any) -> Batch:
    """Create a Batch transaction with sane defaults."""
    return await generate_txn(ctx, "Batch", **overrides)


async def create_amm_create(ctx: TxnContext, **overrides: Any) -> AMMCreate:
    """Create an AMMCreate transaction with sane defaults."""
    return await generate_txn(ctx, "AMMCreate", **overrides)


def update_transaction(transaction: Transaction, **kwargs) -> Transaction:
    """Update an existing transaction with new fields."""
    payload = transaction.to_xrpl()
    payload.update(kwargs)
    return type(transaction).from_xrpl(payload)

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
    Batch,
    BatchFlag,
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
    TicketCreate,
    Transaction,
    TrustSet,
)
from xrpl.transaction import transaction_json_to_binary_codec_form
from xrpl.wallet import Wallet

from workload.randoms import randrange

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
    amm_pools: set[frozenset[str]] | None = None  # AMM pools (asset pairs)
    nfts: dict[str, str] | None = None  # NFTs: {nft_id: owner}
    offers: dict[str, dict] | None = None  # Offers: {offer_id: {type, owner, ...}}
    tickets: dict[str, set[int]] | None = None  # Tickets: {account: {ticket_seq, ...}}
    balances: dict[str, dict[str | tuple[str, str], float]] | None = None  # In-memory balance tracking

    def rand_accounts(self, n: int, omit: list[str] | None = None) -> list["Wallet"]:
        """Pick n unique random accounts, optionally excluding addresses.

        Args:
            n: Number of unique accounts to return.
            omit: List of addresses to exclude (e.g., currency issuers to prevent temDST_IS_SRC).

        Returns:
            List of n unique Wallets.
        """
        available = self.wallets if omit is None else [w for w in self.wallets if w.address not in omit]
        if len(available) < n:
            raise ValueError(f"Need {n} accounts but only {len(available)} available after excluding {omit}")
        return sample(available, n)

    def rand_account(self, omit: list[str] | None = None) -> "Wallet":
        """Pick a single random account, optionally excluding addresses.

        Args:
            omit: List of addresses to exclude (e.g., currency issuers to prevent temDST_IS_SRC).

        Returns:
            Single Wallet.
        """
        return self.rand_accounts(1, omit)[0]

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
    wl = list(ctx.wallets)
    if len(wl) >= 2:
        src, dst = sample(wl, 2)
    else:
        src = wl[0] if wl else ctx.funding_wallet
        dst = ctx.funding_wallet if ctx.funding_wallet is not src else src

    from random import random

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
    from random import random

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
    from random import random

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
    from random import random

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
    "Batch": (_build_batch, Batch),
}


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

        disabled_types = ctx.config.get("transactions", {}).get("disabled", [])
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

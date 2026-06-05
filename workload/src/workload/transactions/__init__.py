"""Transaction registry — single source of truth for all transaction types.

Every transaction type is defined here with its:
- name: assertion name (used in TX_TYPES, workload::seen, etc.)
- path: HTTP endpoint path
- handler: async function to call
- args: lambda taking Workload, returning args tuple for handler
- state_updater: optional (workload, tx_json, meta) callback for WS listener

To add a new transaction type: add one entry to REGISTRY below.
app.py, assertions.py, and ws_listener.py all read from this.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from workload.app import Workload

# ── State updater helpers ────────────────────────────────────────────
# Each handles a validated tesSUCCESS by updating workload tracking lists.
import xrpl.models
from xrpl.models import IssuedCurrency
from xrpl.models.currencies import MPTCurrency

from workload.models import (
    AMM,
    DID,
    NFT,
    Check,
    ConfidentialHolder,
    Credential,
    Delegate,
    Escrow,
    Loan,
    LoanBroker,
    MPTokenIssuance,
    NFTOffer,
    PaymentChannel,
    PermissionedDomain,
    TrustLine,
    Vault,
)
from workload.transactions.account_delete import account_delete
from workload.transactions.account_set import account_set_random
from workload.transactions.amm import (
    amm_bid,
    amm_create,
    amm_delete,
    amm_deposit,
    amm_vote,
    amm_withdraw,
)
from workload.transactions.batch import batch_random
from workload.transactions.checks import check_cancel, check_cash, check_create
from workload.transactions.clawback import clawback
from workload.transactions.confidential_mpt import (
    conf_mpt_clawback,
    conf_mpt_convert,
    conf_mpt_convert_back,
    conf_mpt_merge_inbox,
    conf_mpt_send,
)
from workload.transactions.credentials import (
    credential_accept,
    credential_create,
    credential_delete,
)
from workload.transactions.delegation import delegate_set
from workload.transactions.did import did_delete, did_set
from workload.transactions.domains import permissioned_domain_delete, permissioned_domain_set
from workload.transactions.escrow import escrow_cancel, escrow_create, escrow_finish
from workload.transactions.lending import (
    loan_broker_cover_deposit,
    loan_broker_cover_withdraw,
    loan_broker_delete,
    loan_broker_set,
    loan_delete,
    loan_manage,
    loan_pay,
    loan_set,
)
from workload.transactions.mpt import mpt_authorize, mpt_create, mpt_destroy, mpt_issuance_set
from workload.transactions.nft import (
    nftoken_accept_offer,
    nftoken_burn,
    nftoken_cancel_offer,
    nftoken_create_offer,
    nftoken_mint,
    nftoken_modify,
)
from workload.transactions.offers import offer_cancel, offer_create
from workload.transactions.payment_channels import (
    channel_claim,
    channel_create,
    channel_fund,
)
from workload.transactions.payments import payment_random
from workload.transactions.set_regular_key import set_regular_key
from workload.transactions.signer_list_set import signer_list_set
from workload.transactions.tickets import ticket_create, ticket_use
from workload.transactions.trustlines import trustline_create
from workload.transactions.vaults import (
    vault_clawback,
    vault_create,
    vault_delete,
    vault_deposit,
    vault_set,
    vault_withdraw,
)


def _extract_created_id(meta: dict, entry_type: str) -> str | None:
    for node in meta.get("AffectedNodes", []):
        created = node.get("CreatedNode", {})
        if created.get("LedgerEntryType") == entry_type:
            return created.get("LedgerIndex")
    return None


def _extract_deleted_id(meta: dict, entry_type: str) -> str | None:
    for node in meta.get("AffectedNodes", []):
        deleted = node.get("DeletedNode", {})
        if deleted.get("LedgerEntryType") == entry_type:
            return deleted.get("LedgerIndex")
    return None


def _on_trust_set(w: Workload, tx: dict, meta: dict) -> None:
    limit = tx.get("LimitAmount", {})
    if isinstance(limit, dict):
        w.trust_lines.append(
            TrustLine(
                account_a=tx["Account"],
                account_b=limit.get("issuer", ""),
                currency=limit.get("currency", ""),
            )
        )


def _parse_asset(
    raw: dict,
) -> IssuedCurrency | MPTCurrency | xrpl.models.XRP:
    """Parse an Asset field from a transaction JSON into an xrpl-py model."""
    if "mpt_issuance_id" in raw:
        return MPTCurrency(mpt_issuance_id=raw["mpt_issuance_id"])
    if "issuer" in raw:
        return IssuedCurrency(currency=raw.get("currency", ""), issuer=raw["issuer"])
    return xrpl.models.XRP()


def _on_vault_create(w: Workload, tx: dict, meta: dict) -> None:
    vault_id = _extract_created_id(meta, "Vault")
    if vault_id:
        asset = _parse_asset(tx.get("Asset", {}))
        w.vaults.append(Vault(owner=tx["Account"], vault_id=vault_id, asset=asset))


def _on_vault_delete(w: Workload, tx: dict, meta: dict) -> None:
    vault_id = _extract_deleted_id(meta, "Vault")
    if vault_id:
        w.vaults[:] = [v for v in w.vaults if v.vault_id != vault_id]
        w.deleted_vault_ids.append(vault_id)


def _extract_amount(tx: dict) -> int:
    """Extract integer amount from a transaction's Amount field (drops or IOU/MPT value)."""
    amt = tx.get("Amount", "0")
    if isinstance(amt, str):
        return int(amt)
    return int(amt.get("value", "0"))


def _find_vault(w: Workload, vault_id: str) -> Vault | None:
    return next((v for v in w.vaults if v.vault_id == vault_id), None)


def _on_vault_deposit(w: Workload, tx: dict, meta: dict) -> None:
    vault = _find_vault(w, tx.get("VaultID", ""))
    if vault:
        vault.balance += _extract_amount(tx)
        vault.shareholders.add(tx["Account"])


def _on_vault_withdraw(w: Workload, tx: dict, meta: dict) -> None:
    vault = _find_vault(w, tx.get("VaultID", ""))
    if vault:
        vault.balance = max(0, vault.balance - _extract_amount(tx))


def _on_vault_clawback(w: Workload, tx: dict, meta: dict) -> None:
    vault = _find_vault(w, tx.get("VaultID", ""))
    if vault:
        vault.balance = max(0, vault.balance - _extract_amount(tx))


def _on_nftoken_mint(w: Workload, tx: dict, meta: dict) -> None:
    nftoken_id = meta.get("nftoken_id")
    if nftoken_id:
        account = tx["Account"]
        w.nfts.append(NFT(owner=account, nftoken_id=nftoken_id))
        if account in w.accounts:
            w.accounts[account].nfts.add(nftoken_id)


def _on_nftoken_burn(w: Workload, tx: dict, meta: dict) -> None:
    nftoken_id = tx.get("NFTokenID")
    if nftoken_id:
        w.nfts[:] = [n for n in w.nfts if n.nftoken_id != nftoken_id]
        for acc in w.accounts.values():
            acc.nfts.discard(nftoken_id)


def _on_nftoken_create_offer(w: Workload, tx: dict, meta: dict) -> None:
    offer_id = _extract_created_id(meta, "NFTokenOffer")
    if offer_id:
        w.nft_offers.append(
            NFTOffer(
                creator=tx["Account"],
                offer_id=offer_id,
                nftoken_id=tx.get("NFTokenID", ""),
                is_sell=bool(tx.get("Flags", 0) & 1),
            )
        )


def _on_nftoken_cancel_offer(w: Workload, tx: dict, meta: dict) -> None:
    removed = set(tx.get("NFTokenOffers", []))
    if removed:
        w.nft_offers[:] = [o for o in w.nft_offers if o.offer_id not in removed]


def _on_mpt_create(w: Workload, tx: dict, meta: dict) -> None:
    # mpt_issuance_id is at the top level of meta (like nftoken_id for NFTs)
    mpt_id = meta.get("mpt_issuance_id")
    if mpt_id:
        w.mpt_issuances.append(MPTokenIssuance(issuer=tx["Account"], mpt_issuance_id=mpt_id))


def _on_mpt_destroy(w: Workload, tx: dict, meta: dict) -> None:
    mpt_id = tx.get("MPTokenIssuanceID")
    if mpt_id:
        w.mpt_issuances[:] = [m for m in w.mpt_issuances if m.mpt_issuance_id != mpt_id]


def _on_credential_create(w: Workload, tx: dict, meta: dict) -> None:
    w.credentials.append(
        Credential(
            issuer=tx["Account"],
            subject=tx.get("Subject", ""),
            credential_type=tx.get("CredentialType", ""),
        )
    )


def _on_credential_delete(w: Workload, tx: dict, meta: dict) -> None:
    subject = tx.get("Subject", "")
    issuer = tx.get("Issuer", tx.get("Account", ""))
    cred_type = tx.get("CredentialType", "")
    w.credentials[:] = [
        c
        for c in w.credentials
        if not (c.issuer == issuer and c.subject == subject and c.credential_type == cred_type)
    ]


def _on_ticket_create(w: Workload, tx: dict, meta: dict) -> None:
    account = tx["Account"]
    seq = tx.get("Sequence", 0)
    count = tx.get("TicketCount", 0)
    if account in w.accounts and seq and count:
        w.accounts[account].tickets.update(range(seq + 1, seq + 1 + count))


def _on_domain_set(w: Workload, tx: dict, meta: dict) -> None:
    domain_id = _extract_created_id(meta, "PermissionedDomain")
    if domain_id:
        w.domains.append(PermissionedDomain(owner=tx["Account"], domain_id=domain_id))


def _on_domain_delete(w: Workload, tx: dict, meta: dict) -> None:
    domain_id = _extract_deleted_id(meta, "PermissionedDomain")
    if domain_id:
        w.domains[:] = [d for d in w.domains if d.domain_id != domain_id]


def _on_loan_broker_set(w: Workload, tx: dict, meta: dict) -> None:
    broker_id = _extract_created_id(meta, "LoanBroker")
    if broker_id:
        w.loan_brokers.append(
            LoanBroker(
                owner=tx["Account"], loan_broker_id=broker_id, vault_id=tx.get("VaultID", "")
            )
        )


def _on_loan_broker_delete(w: Workload, tx: dict, meta: dict) -> None:
    broker_id = _extract_deleted_id(meta, "LoanBroker")
    if broker_id:
        w.loan_brokers[:] = [b for b in w.loan_brokers if b.loan_broker_id != broker_id]
        w.deleted_broker_ids.append(broker_id)


def _find_broker(w: Workload, broker_id: str) -> LoanBroker | None:
    return next((b for b in w.loan_brokers if b.loan_broker_id == broker_id), None)


def _find_loan(w: Workload, loan_id: str) -> Loan | None:
    return next((loan for loan in w.loans if loan.loan_id == loan_id), None)


def _on_loan_broker_cover_deposit(w: Workload, tx: dict, meta: dict) -> None:
    broker = _find_broker(w, tx.get("LoanBrokerID", ""))
    if broker:
        broker.cover_balance += _extract_amount(tx)


def _on_loan_broker_cover_withdraw(w: Workload, tx: dict, meta: dict) -> None:
    broker = _find_broker(w, tx.get("LoanBrokerID", ""))
    if broker:
        broker.cover_balance = max(0, broker.cover_balance - _extract_amount(tx))


def _on_loan_set(w: Workload, tx: dict, meta: dict) -> None:
    loan_id = _extract_created_id(meta, "Loan")
    if loan_id:
        principal = int(tx.get("PrincipalRequested", "0"))
        w.loans.append(
            Loan(
                borrower=tx["Account"],
                loan_id=loan_id,
                loan_broker_id=tx.get("LoanBrokerID", ""),
                principal=principal,
            )
        )


def _on_loan_delete(w: Workload, tx: dict, meta: dict) -> None:
    loan_id = _extract_deleted_id(meta, "Loan")
    if loan_id:
        w.loans[:] = [loan for loan in w.loans if loan.loan_id != loan_id]
        w.deleted_loan_ids.append(loan_id)


def _on_loan_manage(w: Workload, tx: dict, meta: dict) -> None:
    loan = _find_loan(w, tx.get("LoanID", ""))
    if not loan:
        return
    flags = tx.get("Flags", 0)
    # TF_LOAN_DEFAULT = 0x00010000, TF_LOAN_IMPAIR = 0x00020000, TF_LOAN_UNIMPAIR = 0x00040000
    if flags & 0x00010000:
        loan.is_defaulted = True
    if flags & 0x00020000:
        loan.is_impaired = True
    if flags & 0x00040000:
        loan.is_impaired = False


def _on_loan_pay(w: Workload, tx: dict, meta: dict) -> None:
    loan = _find_loan(w, tx.get("LoanID", ""))
    if loan:
        loan.principal = max(0, loan.principal - _extract_amount(tx))


def _on_account_delete(w: Workload, tx: dict, meta: dict) -> None:
    deleted = tx.get("Account", "")
    if deleted and deleted in w.accounts:
        del w.accounts[deleted]


def _on_channel_create(w: Workload, tx: dict, meta: dict) -> None:
    channel_id = _extract_created_id(meta, "PayChannel")
    if not channel_id:
        return
    tx_hash = tx.get("hash", "")
    # Update placeholder channel_id with real ledger ID
    for ch in w.payment_channels:
        if ch.channel_id == tx_hash:
            ch.channel_id = channel_id
            return
    w.payment_channels.append(
        PaymentChannel(
            channel_id=channel_id,
            source=tx.get("Account", ""),
            destination=tx.get("Destination", ""),
            amount=str(tx.get("Amount", "0")),
            settle_delay=tx.get("SettleDelay", 0),
        )
    )


def _on_channel_fund(w: Workload, tx: dict, meta: dict) -> None:
    # Fund doesn't create/delete — just adds XRP to existing channel
    pass


def _on_channel_claim(w: Workload, tx: dict, meta: dict) -> None:
    # If the channel was closed by this claim, remove it
    deleted_id = _extract_deleted_id(meta, "PayChannel")
    if deleted_id:
        w.payment_channels[:] = [ch for ch in w.payment_channels if ch.channel_id != deleted_id]


def _on_check_create(w: Workload, tx: dict, meta: dict) -> None:
    check_id = _extract_created_id(meta, "Check")
    if not check_id:
        return
    tx_hash = tx.get("hash", "")
    # Update the placeholder check_id (tx hash) with real ledger check_id
    for c in w.checks:
        if c.check_id == tx_hash:
            c.check_id = check_id
            return
    # If not found optimistically, add it
    w.checks.append(
        Check(
            check_id=check_id,
            creator=tx.get("Account", ""),
            destination=tx.get("Destination", ""),
            send_max=str(tx.get("SendMax", "0")),
        )
    )


def _on_check_cash(w: Workload, tx: dict, meta: dict) -> None:
    deleted_id = _extract_deleted_id(meta, "Check")
    if deleted_id:
        w.checks[:] = [c for c in w.checks if c.check_id != deleted_id]


def _on_check_cancel(w: Workload, tx: dict, meta: dict) -> None:
    deleted_id = _extract_deleted_id(meta, "Check")
    if deleted_id:
        w.checks[:] = [c for c in w.checks if c.check_id != deleted_id]


def _on_escrow_create(w: Workload, tx: dict, meta: dict) -> None:
    """Confirm escrow creation — match by owner+sequence and keep fulfillment."""
    escrow_id = _extract_created_id(meta, "Escrow")
    if not escrow_id:
        return
    owner = tx.get("Account", "")
    seq = tx.get("Sequence", 0)
    # The Escrow was already appended optimistically by escrow_create handler
    # with sequence = tx sequence. Just confirm it exists.
    for e in w.escrows:
        if e.owner == owner and e.sequence == seq:
            return
    # If not found (edge case), add without fulfillment
    w.escrows.append(
        Escrow(
            owner=owner,
            destination=tx.get("Destination", ""),
            sequence=seq,
            condition=tx.get("Condition"),
            fulfillment=None,
            finish_after=tx.get("FinishAfter"),
            cancel_after=tx.get("CancelAfter"),
        )
    )


def _on_escrow_finish(w: Workload, tx: dict, meta: dict) -> None:
    deleted_id = _extract_deleted_id(meta, "Escrow")
    if deleted_id:
        owner = tx.get("Owner", "")
        seq = tx.get("OfferSequence", 0)
        w.escrows[:] = [e for e in w.escrows if not (e.owner == owner and e.sequence == seq)]


def _on_escrow_cancel(w: Workload, tx: dict, meta: dict) -> None:
    deleted_id = _extract_deleted_id(meta, "Escrow")
    if deleted_id:
        owner = tx.get("Owner", "")
        seq = tx.get("OfferSequence", 0)
        w.escrows[:] = [e for e in w.escrows if not (e.owner == owner and e.sequence == seq)]


def _on_did_set(w: Workload, tx: dict, meta: dict) -> None:
    """Track DID in w.dids (idempotent)."""
    account = tx.get("Account", "")
    if not account:
        return
    if not any(d.account == account for d in w.dids):
        w.dids.append(DID(account=account))


def _on_did_delete(w: Workload, tx: dict, meta: dict) -> None:
    """Remove DID from w.dids."""
    account = tx.get("Account", "")
    w.dids[:] = [d for d in w.dids if d.account != account]


def _on_delegate_set(w: Workload, tx: dict, meta: dict) -> None:
    source = tx.get("Account", "")
    delegate_addr = tx.get("Authorize", "")
    perms_raw = tx.get("Permissions", [])
    # Only keep permission values that are transaction type names;
    # GranularPermission values (e.g. "TrustlineAuthorize") will never
    # match a tx_type in maybe_delegate, so filter them out.
    tx_type_names = set(TX_TYPES)
    permissions = []
    for p in perms_raw:
        pv = p.get("Permission", {}).get("PermissionValue", "")
        if pv and pv in tx_type_names:
            permissions.append(pv)
    if not permissions:
        return
    # Replace existing delegation from same source to same delegate
    w.delegates[:] = [
        d for d in w.delegates if not (d.source == source and d.delegate_address == delegate_addr)
    ]
    w.delegates.append(
        Delegate(
            source=source,
            delegate_address=delegate_addr,
            permissions=permissions,
        )
    )


def _on_amm_create(w: Workload, tx: dict, meta: dict) -> None:
    amm_id = _extract_created_id(meta, "AMM")
    if amm_id:
        # Parse both assets from the AMMCreate transaction
        asset1_raw = tx.get("Amount", {})
        asset2_raw = tx.get("Amount2", {})
        assets = []
        for raw in [asset1_raw, asset2_raw]:
            if isinstance(raw, dict) and "currency" in raw:
                assets.append(
                    IssuedCurrency(
                        currency=raw["currency"],
                        issuer=raw.get("issuer", ""),
                    )
                )
            elif isinstance(raw, str):
                assets.append(xrpl.models.XRP())
        # Extract LP token from created AMM node
        lp_token = []
        for node in meta.get("AffectedNodes", []):
            created = node.get("CreatedNode", {})
            if created.get("LedgerEntryType") == "AMM":
                lp_raw = created.get("NewFields", {}).get("LPTokenBalance", {})
                if lp_raw and "currency" in lp_raw:
                    lp_token.append(
                        IssuedCurrency(currency=lp_raw["currency"], issuer=lp_raw.get("issuer", ""))
                    )
        w.amms.append(AMM(account=tx["Account"], assets=assets, lp_token=lp_token))


def _on_amm_delete(w: Workload, tx: dict, meta: dict) -> None:
    amm_id = _extract_deleted_id(meta, "AMM")
    if amm_id:
        # Match by asset pair — the deleter can be anyone, not just the creator.
        tx_assets = {_parse_asset(tx.get("Asset", {})), _parse_asset(tx.get("Asset2", {}))}
        w.amms[:] = [a for a in w.amms if set(a.assets) != tx_assets]


def _on_offer_create(w: Workload, tx: dict, meta: dict) -> None:
    offer_id = _extract_created_id(meta, "Offer")
    if offer_id:
        w.offers.append(
            {
                "account": tx.get("Account", ""),
                "sequence": tx.get("Sequence", 0),
                "offer_id": offer_id,
            }
        )


def _on_offer_cancel(w: Workload, tx: dict, meta: dict) -> None:
    """Remove an explicitly cancelled offer from state.

    NOTE: offers also disappear via fill and expiry, which don't trigger
    this handler. Stale entries will accumulate in w.offers — the cancel-valid
    path may pick already-gone offers and get tecNO_OFFER. This is acceptable
    for fuzzing purposes (exercises error paths).
    """
    deleted_id = _extract_deleted_id(meta, "Offer")
    if deleted_id:
        w.offers[:] = [o for o in w.offers if o.get("offer_id") != deleted_id]


# ── Confidential MPT state updaters ──────────────────────────────────


def _find_conf_issuance(w: Workload, mpt_id: str):
    """Find the ConfidentialMPTIssuance matching *mpt_id*, or ``None``."""
    return next(
        (ci for ci in w.confidential_mpt_issuances if ci.mpt_issuance_id == mpt_id),
        None,
    )


def _on_conf_convert(w: Workload, tx: dict, meta: dict) -> None:
    """Convert: holder moved public MPT into confidential balance."""
    ci = _find_conf_issuance(w, tx.get("MPTokenIssuanceID", ""))
    if not ci:
        return
    account = tx["Account"]
    amount = int(tx.get("MPTAmount", 0))
    holder = ci.holders.get(account)
    if holder:
        holder.spending_balance += amount
        holder.version += 1
    else:
        ci.holders[account] = ConfidentialHolder(
            address=account,
            spending_balance=amount,
            version=1,
        )


def _on_conf_merge_inbox(w: Workload, tx: dict, meta: dict) -> None:
    """MergeInbox: inbox → spending for the holder.

    Always bump version on tesSUCCESS — even if our local inbox_balance
    is 0 (an external Send we didn't track may have funded it).
    """
    ci = _find_conf_issuance(w, tx.get("MPTokenIssuanceID", ""))
    if not ci:
        return
    holder = ci.holders.get(tx["Account"])
    if holder:
        holder.spending_balance += holder.inbox_balance
        holder.inbox_balance = 0
        holder.version += 1


def _on_conf_send(w: Workload, tx: dict, meta: dict) -> None:
    """Send: sender spending balance down, destination inbox up.

    The plaintext amount isn't in the validated tx (it's encrypted).
    We recover it from the pending map populated at submission time.

    Only the sender's version increments (spending balance changed).
    The destination receives into inbox which doesn't affect their
    version counter — that bumps on MergeInbox.
    """
    from workload.transactions.confidential_mpt import _pending_send_amounts

    ci = _find_conf_issuance(w, tx.get("MPTokenIssuanceID", ""))
    if not ci:
        return
    account = tx["Account"]
    seq = tx.get("Sequence", 0)
    mpt_id = tx.get("MPTokenIssuanceID", "")

    # Pop the stashed amount from submission time
    pending = _pending_send_amounts.pop((account, seq, mpt_id), None)

    sender = ci.holders.get(account)
    if sender:
        if pending:
            amount, dest_addr = pending
            sender.spending_balance = max(0, sender.spending_balance - amount)
            dest = ci.holders.get(dest_addr)
            if dest:
                dest.inbox_balance += amount
        sender.version += 1


def _on_conf_convert_back(w: Workload, tx: dict, meta: dict) -> None:
    """ConvertBack: holder moved confidential back to public."""
    ci = _find_conf_issuance(w, tx.get("MPTokenIssuanceID", ""))
    if not ci:
        return
    account = tx["Account"]
    amount = int(tx.get("MPTAmount", 0))
    holder = ci.holders.get(account)
    if holder:
        holder.spending_balance = max(0, holder.spending_balance - amount)
        holder.version += 1


def _on_conf_clawback(w: Workload, tx: dict, meta: dict) -> None:
    """Clawback: issuer reclaimed confidential balance from holder."""
    ci = _find_conf_issuance(w, tx.get("MPTokenIssuanceID", ""))
    if not ci:
        return
    holder_addr = tx.get("Holder", "")
    amount = int(tx.get("MPTAmount", 0))
    holder = ci.holders.get(holder_addr)
    if holder:
        holder.spending_balance = max(0, holder.spending_balance - amount)
        holder.version += 1


# ── Registry ─────────────────────────────────────────────────────────
# (name, path, handler, args_fn, state_updater_or_None)

REGISTRY = [
    (
        "NFTokenMint",
        "/nft/mint/random",
        nftoken_mint,
        lambda w: (w.accounts, w.nfts, w.client),
        _on_nftoken_mint,
    ),
    (
        "NFTokenBurn",
        "/nft/burn/random",
        nftoken_burn,
        lambda w: (w.accounts, w.nfts, w.client),
        _on_nftoken_burn,
    ),
    (
        "NFTokenModify",
        "/nft/modify/random",
        nftoken_modify,
        lambda w: (w.accounts, w.nfts, w.client),
        None,
    ),
    (
        "NFTokenCreateOffer",
        "/nft/create_offer/random",
        nftoken_create_offer,
        lambda w: (w.accounts, w.nfts, w.nft_offers, w.client),
        _on_nftoken_create_offer,
    ),
    (
        "NFTokenCancelOffer",
        "/nft/cancel_offer/random",
        nftoken_cancel_offer,
        lambda w: (w.accounts, w.nft_offers, w.client),
        _on_nftoken_cancel_offer,
    ),
    (
        "NFTokenAcceptOffer",
        "/nft/accept_offer/random",
        nftoken_accept_offer,
        lambda w: (w.accounts, w.nfts, w.nft_offers, w.client),
        None,
    ),
    (
        "AccountSet",
        "/account/set/random",
        account_set_random,
        lambda w: (w.accounts, w.client),
        None,
    ),
    (
        "TrustSet",
        "/trustline/create/random",
        trustline_create,
        lambda w: (w.accounts, w.trust_lines, w.client),
        _on_trust_set,
    ),
    (
        "Payment",
        "/payment/random",
        payment_random,
        lambda w: (w.accounts, w.trust_lines, w.mpt_issuances, w.client),
        None,
    ),
    (
        "TicketCreate",
        "/tickets/create/random",
        ticket_create,
        lambda w: (w.accounts, w.client),
        _on_ticket_create,
    ),
    (
        "TicketUse",
        "/tickets/use/random",
        ticket_use,
        lambda w: (w.accounts, w.client),
        None,
    ),
    (
        "Batch",
        "/batch/random",
        batch_random,
        lambda w: (w.accounts, w.client),
        None,
    ),
    (
        "MPTokenIssuanceCreate",
        "/mpt/create/random",
        mpt_create,
        lambda w: (w.accounts, w.mpt_issuances, w.client),
        _on_mpt_create,
    ),
    (
        "MPTokenAuthorize",
        "/mpt/authorize/random",
        mpt_authorize,
        lambda w: (w.accounts, w.mpt_issuances, w.client),
        None,
    ),
    (
        "MPTokenIssuanceSet",
        "/mpt/set/random",
        mpt_issuance_set,
        lambda w: (w.accounts, w.mpt_issuances, w.client),
        None,
    ),
    (
        "MPTokenIssuanceDestroy",
        "/mpt/destroy/random",
        mpt_destroy,
        lambda w: (w.accounts, w.mpt_issuances, w.client),
        _on_mpt_destroy,
    ),
    (
        "CredentialCreate",
        "/credential/create/random",
        credential_create,
        lambda w: (w.accounts, w.credentials, w.client),
        _on_credential_create,
    ),
    (
        "CredentialAccept",
        "/credential/accept/random",
        credential_accept,
        lambda w: (w.accounts, w.credentials, w.client),
        None,
    ),
    (
        "CredentialDelete",
        "/credential/delete/random",
        credential_delete,
        lambda w: (w.accounts, w.credentials, w.client),
        _on_credential_delete,
    ),
    (
        "VaultCreate",
        "/vault/create/random",
        vault_create,
        lambda w: (w.accounts, w.vaults, w.trust_lines, w.mpt_issuances, w.client),
        _on_vault_create,
    ),
    (
        "VaultDeposit",
        "/vault/deposit/random",
        vault_deposit,
        lambda w: (w.accounts, w.vaults, w.client),
        _on_vault_deposit,
    ),
    (
        "VaultWithdraw",
        "/vault/withdraw/random",
        vault_withdraw,
        lambda w: (w.accounts, w.vaults, w.client),
        _on_vault_withdraw,
    ),
    (
        "VaultSet",
        "/vault/set/random",
        vault_set,
        lambda w: (w.accounts, w.vaults, w.client),
        None,
    ),
    (
        "VaultDelete",
        "/vault/delete/random",
        vault_delete,
        lambda w: (w.accounts, w.vaults, w.client),
        _on_vault_delete,
    ),
    (
        "VaultClawback",
        "/vault/clawback/random",
        vault_clawback,
        lambda w: (w.accounts, w.vaults, w.client),
        _on_vault_clawback,
    ),
    (
        "PermissionedDomainSet",
        "/domain/set/random",
        permissioned_domain_set,
        lambda w: (w.accounts, w.domains, w.client),
        _on_domain_set,
    ),
    (
        "PermissionedDomainDelete",
        "/domain/delete/random",
        permissioned_domain_delete,
        lambda w: (w.accounts, w.domains, w.client),
        _on_domain_delete,
    ),
    (
        "DelegateSet",
        "/delegate/set/random",
        delegate_set,
        lambda w: (w.accounts, w.client),
        _on_delegate_set,
    ),
    (
        "AMMCreate",
        "/amm/create/random",
        amm_create,
        lambda w: (w.accounts, w.amms, w.trust_lines, w.client),
        _on_amm_create,
    ),
    (
        "AMMDeposit",
        "/amm/deposit/random",
        amm_deposit,
        lambda w: (w.accounts, w.amms, w.client),
        None,
    ),
    (
        "AMMWithdraw",
        "/amm/withdraw/random",
        amm_withdraw,
        lambda w: (w.accounts, w.amms, w.client),
        None,
    ),
    (
        "AMMVote",
        "/amm/vote/random",
        amm_vote,
        lambda w: (w.accounts, w.amms, w.trust_lines, w.client),
        None,
    ),
    (
        "AMMBid",
        "/amm/bid/random",
        amm_bid,
        lambda w: (w.accounts, w.amms, w.client),
        None,
    ),
    (
        "AMMDelete",
        "/amm/delete/random",
        amm_delete,
        lambda w: (w.accounts, w.amms, w.client),
        _on_amm_delete,
    ),
    (
        "OfferCreate",
        "/offer/create/random",
        offer_create,
        lambda w: (w.accounts, w.amms, w.trust_lines, w.client),
        _on_offer_create,
    ),
    (
        "OfferCancel",
        "/offer/cancel/random",
        offer_cancel,
        lambda w: (w.accounts, w.offers, w.client),
        _on_offer_cancel,
    ),
    (
        "CheckCreate",
        "/check/create/random",
        check_create,
        lambda w: (w.accounts, w.checks, w.client),
        _on_check_create,
    ),
    (
        "PaymentChannelCreate",
        "/channel/create/random",
        channel_create,
        lambda w: (w.accounts, w.payment_channels, w.client),
        _on_channel_create,
    ),
    (
        "PaymentChannelFund",
        "/channel/fund/random",
        channel_fund,
        lambda w: (w.accounts, w.payment_channels, w.client),
        _on_channel_fund,
    ),
    (
        "PaymentChannelClaim",
        "/channel/claim/random",
        channel_claim,
        lambda w: (w.accounts, w.payment_channels, w.client),
        _on_channel_claim,
    ),
    (
        "CheckCash",
        "/check/cash/random",
        check_cash,
        lambda w: (w.accounts, w.checks, w.client),
        _on_check_cash,
    ),
    (
        "CheckCancel",
        "/check/cancel/random",
        check_cancel,
        lambda w: (w.accounts, w.checks, w.client),
        _on_check_cancel,
    ),
    (
        "Clawback",
        "/clawback/random",
        clawback,
        lambda w: (w.accounts, w.trust_lines, w.mpt_issuances, w.client),
        None,
    ),
    (
        "SetRegularKey",
        "/set_regular_key/random",
        set_regular_key,
        lambda w: (w.accounts, w.client),
        None,
    ),
    (
        "SignerListSet",
        "/signer_list_set/random",
        signer_list_set,
        lambda w: (w.accounts, w.client),
        None,
    ),
    (
        "AccountDelete",
        "/account_delete/random",
        account_delete,
        lambda w: (w.accounts, w.protected_accounts, w.client),
        _on_account_delete,
    ),
    (
        "EscrowCreate",
        "/escrow/create/random",
        escrow_create,
        lambda w: (w.accounts, w.escrows, w.client),
        _on_escrow_create,
    ),
    (
        "EscrowFinish",
        "/escrow/finish/random",
        escrow_finish,
        lambda w: (w.accounts, w.escrows, w.client),
        _on_escrow_finish,
    ),
    (
        "EscrowCancel",
        "/escrow/cancel/random",
        escrow_cancel,
        lambda w: (w.accounts, w.escrows, w.client),
        _on_escrow_cancel,
    ),
    (
        "DIDSet",
        "/did/set/random",
        did_set,
        lambda w: (w.accounts, w.client),
        _on_did_set,
    ),
    (
        "DIDDelete",
        "/did/delete/random",
        did_delete,
        lambda w: (w.accounts, w.dids, w.client),
        _on_did_delete,
    ),
    (
        "LoanBrokerSet",
        "/loan/broker/set/random",
        loan_broker_set,
        lambda w: (w.accounts, w.vaults, w.loan_brokers, w.client),
        _on_loan_broker_set,
    ),
    (
        "LoanBrokerDelete",
        "/loan/broker/delete/random",
        loan_broker_delete,
        lambda w: (w.accounts, w.loan_brokers, w.client),
        _on_loan_broker_delete,
    ),
    (
        "LoanBrokerCoverDeposit",
        "/loan/broker/cover/deposit/random",
        loan_broker_cover_deposit,
        lambda w: (w.accounts, w.loan_brokers, w.client),
        _on_loan_broker_cover_deposit,
    ),
    (
        "LoanBrokerCoverWithdraw",
        "/loan/broker/cover/withdraw/random",
        loan_broker_cover_withdraw,
        lambda w: (w.accounts, w.loan_brokers, w.client),
        _on_loan_broker_cover_withdraw,
    ),
    (
        "LoanSet",
        "/loan/set/random",
        loan_set,
        lambda w: (w.accounts, w.loan_brokers, w.loans, w.client),
        _on_loan_set,
    ),
    (
        "LoanDelete",
        "/loan/delete/random",
        loan_delete,
        lambda w: (w.accounts, w.loans, w.client),
        _on_loan_delete,
    ),
    (
        "LoanManage",
        "/loan/manage/random",
        loan_manage,
        lambda w: (w.accounts, w.loan_brokers, w.loans, w.client),
        _on_loan_manage,
    ),
    (
        "LoanPay",
        "/loan/pay/random",
        loan_pay,
        lambda w: (w.accounts, w.loans, w.client),
        _on_loan_pay,
    ),
    (
        "ConfidentialMPTMergeInbox",
        "/confidential/merge_inbox/random",
        conf_mpt_merge_inbox,
        lambda w: (w.accounts, w.mpt_issuances, w.confidential_mpt_issuances, w.client),
        _on_conf_merge_inbox,
    ),
    (
        "ConfidentialMPTConvert",
        "/confidential/convert/random",
        conf_mpt_convert,
        lambda w: (w.accounts, w.mpt_issuances, w.confidential_mpt_issuances, w.client),
        _on_conf_convert,
    ),
    (
        "ConfidentialMPTSend",
        "/confidential/send/random",
        conf_mpt_send,
        lambda w: (w.accounts, w.mpt_issuances, w.confidential_mpt_issuances, w.client),
        _on_conf_send,
    ),
    (
        "ConfidentialMPTConvertBack",
        "/confidential/convert_back/random",
        conf_mpt_convert_back,
        lambda w: (w.accounts, w.mpt_issuances, w.confidential_mpt_issuances, w.client),
        _on_conf_convert_back,
    ),
    (
        "ConfidentialMPTClawback",
        "/confidential/clawback/random",
        conf_mpt_clawback,
        lambda w: (w.accounts, w.mpt_issuances, w.confidential_mpt_issuances, w.client),
        _on_conf_clawback,
    ),
]

# Derived views for consumers
TX_TYPES = [name for name, *_ in REGISTRY]
STATE_UPDATERS = {name: updater for name, _, _, _, updater in REGISTRY if updater}

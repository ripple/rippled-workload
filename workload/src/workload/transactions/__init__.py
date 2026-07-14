"""Transaction registry — single source of truth for all transaction types."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from workload.app import Workload

# ── State updater helpers ────────────────────────────────────────────
import xrpl.models
from xrpl.models import IssuedCurrency
from xrpl.models.currencies import MPTCurrency
from xrpl.models.transactions import MPTokenIssuanceCreateFlag

from workload import params
from workload.models import (
    AMM,
    DID,
    NFT,
    Check,
    ConfidentialHolder,
    ConfidentialMPTIssuance,
    Credential,
    Delegate,
    Escrow,
    Loan,
    LoanBroker,
    MPTokenIssuance,
    NFTOffer,
    PaymentChannel,
    PermissionedDomain,
    Sponsorship,
    TrustLine,
    UserAccount,
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
from workload.transactions.deposit_preauth import deposit_preauth
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
from workload.transactions.mpt_dex import offer_create_mpt, payment_mpt
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
from workload.transactions.permissioned_dex import (
    offer_create_domain,
    offer_create_hybrid,
    payment_domain,
    payment_domain_xc,
)
from workload.transactions.set_regular_key import set_regular_key
from workload.transactions.signer_list_set import signer_list_set
from workload.transactions.sponsor_malformation import sponsor_malformation
from workload.transactions.sponsorship import (
    payment_sponsored_account,
    sponsorship_set,
    sponsorship_set_delete,
    sponsorship_transfer,
    sponsorship_transfer_account,
)
from workload.transactions.tickets import ticket_create
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
            idx = created.get("LedgerIndex")
            return idx if isinstance(idx, str) else None
    return None


def _extract_deleted_id(meta: dict, entry_type: str) -> str | None:
    for node in meta.get("AffectedNodes", []):
        deleted = node.get("DeletedNode", {})
        if deleted.get("LedgerEntryType") == entry_type:
            idx = deleted.get("LedgerIndex")
            return idx if isinstance(idx, str) else None
    return None


def _find_ripple_state_id(meta: dict, account: str) -> str | None:
    """RippleState's own account fields live inside HighLimit/LowLimit's
    'issuer', not a top-level Account field -- match on either side."""
    for node in meta.get("AffectedNodes", []):
        for event in ("CreatedNode", "ModifiedNode"):
            entry = node.get(event, {})
            if entry.get("LedgerEntryType") != "RippleState":
                continue
            fields = entry.get("NewFields") or entry.get("FinalFields") or {}
            high = fields.get("HighLimit", {})
            low = fields.get("LowLimit", {})
            if high.get("issuer") == account or low.get("issuer") == account:
                idx = entry.get("LedgerIndex")
                return idx if isinstance(idx, str) else None
    return None


def _on_trust_set(w: Workload, tx: dict, meta: dict) -> None:
    limit = tx.get("LimitAmount", {})
    if not isinstance(limit, dict):
        return
    account_a = tx["Account"]
    account_b = limit.get("issuer", "")
    currency = limit.get("currency", "")
    trust_line_id = _find_ripple_state_id(meta, account_a)
    for tl in w.trust_lines:
        if {tl.account_a, tl.account_b} == {account_a, account_b} and tl.currency == currency:
            if trust_line_id:
                tl.trust_line_id = trust_line_id
            return
    w.trust_lines.append(
        TrustLine(
            account_a=account_a,
            account_b=account_b,
            currency=currency,
            trust_line_id=trust_line_id,
        )
    )


def _parse_asset(
    raw: dict | str,
) -> IssuedCurrency | MPTCurrency | xrpl.models.XRP:
    """A plain string (e.g. an XRP drops amount) maps to XRP."""
    if not isinstance(raw, dict):
        return xrpl.models.XRP()
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
    # mpt_issuance_id is at meta top level, like nftoken_id for NFTs
    mpt_id = meta.get("mpt_issuance_id")
    if mpt_id:
        flags = tx.get("Flags", 0)
        issuance = MPTokenIssuance(
            issuer=tx["Account"],
            mpt_issuance_id=mpt_id,
            can_trade=bool(flags & int(MPTokenIssuanceCreateFlag.TF_MPT_CAN_TRADE)),
            can_transfer=bool(flags & int(MPTokenIssuanceCreateFlag.TF_MPT_CAN_TRANSFER)),
            require_auth=bool(flags & int(MPTokenIssuanceCreateFlag.TF_MPT_REQUIRE_AUTH)),
            # lock state set later by setup, not at create
            locked=False,
        )
        w.mpt_issuances.append(issuance)


def _on_mpt_authorize(w: Workload, tx: dict, meta: dict) -> None:
    mpt_id = tx.get("MPTokenIssuanceID")
    if not mpt_id:
        return
    # Holder self-opt-in: submitter holds. Issuer-authorizes mode: Holder field
    # names the account that now holds.
    held = tx.get("Holder") or tx.get("Account", "")
    if not held:
        return
    for m in w.mpt_issuances:
        if m.mpt_issuance_id == mpt_id:
            m.holders.add(held)
            return


def _on_mpt_destroy(w: Workload, tx: dict, meta: dict) -> None:
    mpt_id = tx.get("MPTokenIssuanceID")
    if mpt_id:
        w.mpt_issuances[:] = [m for m in w.mpt_issuances if m.mpt_issuance_id != mpt_id]


# ── Confidential MPT (XLS-0096) state updaters ───────────────────────


def _find_conf_issuance(w: Workload, mpt_id: str) -> ConfidentialMPTIssuance | None:
    return next(
        (ci for ci in w.confidential_mpt_issuances if ci.mpt_issuance_id == mpt_id),
        None,
    )


def _on_conf_convert(w: Workload, tx: dict, meta: dict) -> None:
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
            address=account, spending_balance=amount, version=1
        )


def _on_conf_merge_inbox(w: Workload, tx: dict, meta: dict) -> None:
    ci = _find_conf_issuance(w, tx.get("MPTokenIssuanceID", ""))
    if not ci:
        return
    holder = ci.holders.get(tx["Account"])
    if holder:
        holder.spending_balance += holder.inbox_balance
        holder.inbox_balance = 0
        holder.version += 1


def _on_conf_send(w: Workload, tx: dict, meta: dict) -> None:
    """Amount is encrypted on-chain, so recover it from the submit-time side-channel;
    only the sender's version bumps (dest inbox bumps on MergeInbox)."""
    from workload.transactions.confidential_mpt import _pending_send_amounts

    ci = _find_conf_issuance(w, tx.get("MPTokenIssuanceID", ""))
    if not ci:
        return
    account = tx["Account"]
    key = (account, tx.get("Sequence", 0), tx.get("MPTokenIssuanceID", ""))
    pending = _pending_send_amounts.pop(key, None)
    sender = ci.holders.get(account)
    if not sender:
        return
    if pending:
        amount, dest_addr = pending
        sender.spending_balance = max(0, sender.spending_balance - amount)
        dest = ci.holders.get(dest_addr)
        if dest:
            dest.inbox_balance += amount
    sender.version += 1


def _on_conf_convert_back(w: Workload, tx: dict, meta: dict) -> None:
    ci = _find_conf_issuance(w, tx.get("MPTokenIssuanceID", ""))
    if not ci:
        return
    holder = ci.holders.get(tx["Account"])
    if holder:
        holder.spending_balance = max(0, holder.spending_balance - int(tx.get("MPTAmount", 0)))
        holder.version += 1


def _on_conf_clawback(w: Workload, tx: dict, meta: dict) -> None:
    ci = _find_conf_issuance(w, tx.get("MPTokenIssuanceID", ""))
    if not ci:
        return
    holder = ci.holders.get(tx.get("Holder", ""))
    if holder:
        holder.spending_balance = max(0, holder.spending_balance - int(tx.get("MPTAmount", 0)))
        holder.version += 1


def _on_credential_create(w: Workload, tx: dict, meta: dict) -> None:
    w.credentials.append(
        Credential(
            issuer=tx["Account"],
            subject=tx.get("Subject", ""),
            credential_type=tx.get("CredentialType", ""),
            credential_id=_extract_created_id(meta, "Credential"),
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


def _on_credential_accept(w: Workload, tx: dict, meta: dict) -> None:
    # ModifiedNode only (no created/deleted id); subject is the submitter.
    issuer = tx.get("Issuer", "")
    subject = tx.get("Account", "")
    cred_type = tx.get("CredentialType", "")
    for c in w.credentials:
        if c.issuer == issuer and c.subject == subject and c.credential_type == cred_type:
            c.accepted = True
            break


def _on_ticket_create(w: Workload, tx: dict, meta: dict) -> None:
    account = tx["Account"]
    seq = tx.get("Sequence", 0)
    count = tx.get("TicketCount", 0)
    if account in w.accounts and seq and count:
        w.accounts[account].tickets.update(range(seq + 1, seq + 1 + count))


def _parse_accepted_credentials(tx: dict) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for entry in tx.get("AcceptedCredentials", []):
        cred = entry.get("Credential", entry)
        issuer = cred.get("Issuer", "")
        cred_type = cred.get("CredentialType", "")
        if issuer and cred_type:
            pairs.append((issuer, cred_type))
    return pairs


def _on_domain_set(w: Workload, tx: dict, meta: dict) -> None:
    accepted = _parse_accepted_credentials(tx)
    domain_id = _extract_created_id(meta, "PermissionedDomain")
    if domain_id:
        w.domains.append(
            PermissionedDomain(
                owner=tx["Account"], domain_id=domain_id, accepted_credentials=accepted
            )
        )
        return
    # ModifiedNode: refresh accepted credentials on existing domain.
    existing_id = tx.get("DomainID")
    if existing_id:
        for d in w.domains:
            if d.domain_id == existing_id:
                d.accepted_credentials = accepted
                break


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
    # TF_LOAN_DEFAULT=0x00010000, TF_LOAN_IMPAIR=0x00020000, TF_LOAN_UNIMPAIR=0x00040000
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
    # Deleting a sponsored account dissolves its account-level sponsorship too.
    w.sponsored_accounts.pop(deleted, None)


def _on_channel_create(w: Workload, tx: dict, meta: dict) -> None:
    channel_id = _extract_created_id(meta, "PayChannel")
    if not channel_id:
        return
    tx_hash = tx.get("hash", "")
    # Replace placeholder channel_id (tx hash) with real ledger ID
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
    # Fund neither creates nor deletes a channel — nothing to track.
    pass


def _on_channel_claim(w: Workload, tx: dict, meta: dict) -> None:
    deleted_id = _extract_deleted_id(meta, "PayChannel")
    if deleted_id:
        w.payment_channels[:] = [ch for ch in w.payment_channels if ch.channel_id != deleted_id]


def _on_check_create(w: Workload, tx: dict, meta: dict) -> None:
    check_id = _extract_created_id(meta, "Check")
    if not check_id:
        return
    tx_hash = tx.get("hash", "")
    # Replace placeholder check_id (tx hash) with real ledger check_id
    for c in w.checks:
        if c.check_id == tx_hash:
            c.check_id = check_id
            return
    # check_cash math (int(send_max)) assumes XRP drops; a fuzz-morphed IOU/MPT SendMax
    # is a non-numeric object, so don't track a check we can't cash.
    send_max = tx.get("SendMax")
    if not isinstance(send_max, str):
        return
    w.checks.append(
        Check(
            check_id=check_id,
            creator=tx.get("Account", ""),
            destination=tx.get("Destination", ""),
            send_max=send_max,
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
    # Escrow already appended optimistically by handler (keeps fulfillment);
    # confirm by owner+sequence, only re-add as fallback.
    escrow_id = _extract_created_id(meta, "Escrow")
    if not escrow_id:
        return
    owner = tx.get("Account", "")
    seq = tx.get("Sequence", 0)
    for e in w.escrows:
        if e.owner == owner and e.sequence == seq:
            e.escrow_id = escrow_id
            return
    w.escrows.append(
        Escrow(
            owner=owner,
            destination=tx.get("Destination", ""),
            sequence=seq,
            condition=tx.get("Condition"),
            fulfillment=None,
            finish_after=tx.get("FinishAfter"),
            cancel_after=tx.get("CancelAfter"),
            escrow_id=escrow_id,
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
    account = tx.get("Account", "")
    if not account:
        return
    if not any(d.account == account for d in w.dids):
        w.dids.append(DID(account=account))


def _on_did_delete(w: Workload, tx: dict, meta: dict) -> None:
    account = tx.get("Account", "")
    w.dids[:] = [d for d in w.dids if d.account != account]


def _on_delegate_set(w: Workload, tx: dict, meta: dict) -> None:
    source = tx.get("Account", "")
    delegate_addr = tx.get("Authorize", "")
    perms_raw = tx.get("Permissions", [])
    # GranularPermission values (e.g. "TrustlineAuthorize") never match a
    # tx_type in maybe_delegate, so keep only tx-type-name permissions.
    tx_type_names = set(TX_TYPES)
    permissions = []
    for p in perms_raw:
        pv = p.get("Permission", {}).get("PermissionValue", "")
        if pv and pv in tx_type_names:
            permissions.append(pv)
    if not permissions:
        return
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
        # _parse_asset so MPT amounts (dict, no currency) are tracked too.
        asset1_raw = tx.get("Amount", {})
        asset2_raw = tx.get("Amount2", {})
        assets = [
            _parse_asset(raw) for raw in (asset1_raw, asset2_raw) if isinstance(raw, (dict, str))
        ]
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
        # Match by asset pair — deleter can be anyone, not just the creator.
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
    # Offers also vanish via fill/expiry (no handler), so stale entries
    # accumulate; cancel-valid may then pick a gone offer (tecNO_OFFER), which
    # is acceptable — it exercises error paths.
    deleted_id = _extract_deleted_id(meta, "Offer")
    if deleted_id:
        w.offers[:] = [o for o in w.offers if o.get("offer_id") != deleted_id]


# ── Sponsorship (XLS-68) state updaters ──────────────────────────────


def _sponsorship_ledger_node(meta: dict, event: str) -> dict:
    for node in meta.get("AffectedNodes", []):
        entry = node.get(event, {})
        if entry.get("LedgerEntryType") == "Sponsorship":
            return entry
    return {}


def _sponsorship_fields(entry: dict) -> dict:
    return entry.get("NewFields") or entry.get("FinalFields") or {}


def _find_sponsorship(w: Workload, sponsor: str, sponsee: str) -> Sponsorship | None:
    return next((s for s in w.sponsorships if s.sponsor == sponsor and s.sponsee == sponsee), None)


def _on_sponsorship_set(w: Workload, tx: dict, meta: dict) -> None:
    created = _sponsorship_ledger_node(meta, "CreatedNode")
    if created:
        fields = _sponsorship_fields(created)
        flags = fields.get("Flags", 0)
        w.sponsorships.append(
            Sponsorship(
                sponsor=fields.get("Owner", ""),
                sponsee=fields.get("Sponsee", ""),
                fee_amount=int(fields.get("FeeAmount", 0)),
                max_fee=int(fields["MaxFee"]) if "MaxFee" in fields else None,
                remaining_owner_count=int(fields.get("RemainingOwnerCount", 0)),
                require_sign_for_fee=bool(flags & 0x00010000),
                require_sign_for_reserve=bool(flags & 0x00020000),
            )
        )
        return
    modified = _sponsorship_ledger_node(meta, "ModifiedNode")
    if modified:
        fields = _sponsorship_fields(modified)
        s = _find_sponsorship(w, fields.get("Owner", ""), fields.get("Sponsee", ""))
        if s:
            flags = fields.get("Flags", 0)
            s.fee_amount = int(fields.get("FeeAmount", 0))
            s.max_fee = int(fields["MaxFee"]) if "MaxFee" in fields else None
            s.remaining_owner_count = int(fields.get("RemainingOwnerCount", 0))
            s.require_sign_for_fee = bool(flags & 0x00010000)
            s.require_sign_for_reserve = bool(flags & 0x00020000)
        return
    deleted = _sponsorship_ledger_node(meta, "DeletedNode")
    if deleted:
        fields = _sponsorship_fields(deleted)
        owner, sponsee = fields.get("Owner", ""), fields.get("Sponsee", "")
        w.sponsorships[:] = [
            s for s in w.sponsorships if not (s.sponsor == owner and s.sponsee == sponsee)
        ]


def _on_sponsorship_transfer(w: Workload, tx: dict, meta: dict) -> None:
    """Object-level (ObjectID set) and account-level (absent) flavours share one
    real TransactionType, so both endpoints (SponsorshipTransfer and the
    synthetic SponsorshipTransferAccount bucket) land here. Derived from
    tx-level fields (Account/Sponsee/Sponsor) rather than meta, sidestepping
    the Sponsor-vs-High/LowSponsor field-name split per ledger-entry type
    (see rippled's SponsorHelpers.h getLedgerEntrySponsorField)."""
    flags = tx.get("Flags", 0)
    object_id = tx.get("ObjectID")
    owner = tx.get("Sponsee") or tx.get("Account", "")
    if flags & params.TF_SPONSORSHIP_END:
        if object_id:
            w.sponsored_objects.pop(object_id, None)
        else:
            w.sponsored_accounts.pop(owner, None)
        return
    new_sponsor = tx.get("Sponsor", "")
    if not new_sponsor:
        return
    if object_id:
        w.sponsored_objects[object_id] = (owner, new_sponsor)
    else:
        w.sponsored_accounts[owner] = new_sponsor


_TF_PAYMENT_SPONSOR_CREATED_ACCOUNT = 0x00080000  # PaymentFlag.TF_SPONSOR_CREATED_ACCOUNT


def _on_payment_maybe_sponsored_account(w: Workload, tx: dict, meta: dict) -> None:
    """Attached to the real "Payment" row (not the synthetic PaymentSponsoredAccount
    bucket) since that's rippled's actual TransactionType; a no-op for every
    ordinary payment. Lazy import mirrors _on_conf_send's pattern -- only reached
    when the flag is actually set, i.e. only when the SPONSOR-gated endpoint ran."""
    if not tx.get("Flags", 0) & _TF_PAYMENT_SPONSOR_CREATED_ACCOUNT:
        return
    from workload.transactions.sponsorship import _pending_sponsored_account_wallets

    dest = tx.get("Destination", "")
    wallet = _pending_sponsored_account_wallets.pop(dest, None)
    if wallet is None:
        return
    w.accounts[dest] = UserAccount(wallet=wallet)
    w.sponsored_accounts[dest] = tx.get("Account", "")


# ── Registry ─────────────────────────────────────────────────────────
# (name, path, handler, args_fn, state_updater | None)

Handler = Callable[..., Awaitable[Any]]
# Param is the Workload, but typing it as such forces resolution through the
# app↔registry import cycle (mypy has-type); Any keeps the lambdas checkable.
ArgsFn = Callable[[Any], tuple[Any, ...]]
StateUpdater = Callable[["Workload", dict, dict], None]

_CONFIDENTIAL_REGISTRY_ENTRIES: list[tuple[str, str, Handler, ArgsFn, StateUpdater | None]] = [
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

# SponsorshipSetDelete, SponsorshipTransferAccount, and PaymentSponsoredAccount are
# synthetic buckets on top of the real SponsorshipSet/SponsorshipTransfer/Payment
# types (ws_listener.py maps results back), so they pass state_updater=None here --
# PaymentSponsoredAccount's actual state update lives on the real "Payment" row
# above (_on_payment_maybe_sponsored_account), since STATE_UPDATERS is keyed by
# real TransactionType.
_SPONSOR_REGISTRY_ENTRIES: list[tuple[str, str, Handler, ArgsFn, StateUpdater | None]] = [
    (
        "SponsorshipSet",
        "/sponsorship/set/random",
        sponsorship_set,
        lambda w: (w.accounts, w.sponsorships, w.client),
        _on_sponsorship_set,
    ),
    (
        "SponsorshipSetDelete",
        "/sponsorship/delete/random",
        sponsorship_set_delete,
        lambda w: (w.accounts, w.sponsorships, w.client),
        None,
    ),
    (
        "SponsorshipTransfer",
        "/sponsorship/transfer/random",
        sponsorship_transfer,
        lambda w: (
            w.accounts,
            w.sponsorships,
            w.checks,
            w.escrows,
            w.payment_channels,
            w.trust_lines,
            w.credentials,
            w.sponsored_objects,
            w.offers,
            w.client,
        ),
        _on_sponsorship_transfer,
    ),
    (
        "SponsorshipTransferAccount",
        "/sponsorship/transfer/account/random",
        sponsorship_transfer_account,
        lambda w: (
            w.accounts,
            w.sponsorships,
            w.sponsored_accounts,
            w.protected_accounts,
            w.client,
        ),
        None,
    ),
    (
        "PaymentSponsoredAccount",
        "/payment/sponsored_account/random",
        payment_sponsored_account,
        lambda w: (w.accounts, w.client),
        None,
    ),
    # Reserve/fee sponsorship of any supported tx now rides the submit-time
    # sponsor Modifier (modifiers.py). This lone endpoint carries the sponsor
    # malformations xrpl-py rejects at construction (raw submit, modifier-free);
    # all vectors are preflight tem*, so state_updater=None.
    (
        "SponsorMalformation",
        "/sponsor/malformation/random",
        sponsor_malformation,
        lambda w: (w.accounts, w.client),
        None,
    ),
]

REGISTRY: list[tuple[str, str, Handler, ArgsFn, StateUpdater | None]] = [
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
        _on_payment_maybe_sponsored_account,
    ),
    (
        "TicketCreate",
        "/tickets/create/random",
        ticket_create,
        lambda w: (w.accounts, w.client),
        _on_ticket_create,
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
        _on_mpt_authorize,
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
    *_CONFIDENTIAL_REGISTRY_ENTRIES,
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
        _on_credential_accept,
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
        lambda w: (w.accounts, w.domains, w.credentials, w.client),
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
        lambda w: (w.accounts, w.amms, w.trust_lines, w.mpt_issuances, w.client),
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
    # ── Permissioned DEX (featurePermissionedDEX) ────────────────────
    # Synthetic names; on-ledger type stays OfferCreate/Payment. Real-type
    # updaters track state; ws_listener fires tx_result for these buckets.
    (
        "OfferCreateDomain",
        "/offer/create/domain/random",
        offer_create_domain,
        lambda w: (w.accounts, w.domains, w.credentials, w.amms, w.client),
        None,
    ),
    (
        "OfferCreateHybrid",
        "/offer/create/hybrid/random",
        offer_create_hybrid,
        lambda w: (w.accounts, w.domains, w.credentials, w.amms, w.client),
        None,
    ),
    (
        "PaymentDomain",
        "/payment/domain/random",
        payment_domain,
        lambda w: (w.accounts, w.domains, w.credentials, w.client),
        None,
    ),
    (
        "PaymentDomainXC",
        "/payment/domain/xc/random",
        payment_domain_xc,
        lambda w: (w.accounts, w.domains, w.credentials, w.amms, w.client),
        None,
    ),
    # ── MPT-on-DEX (XLS-82) ──────────────────────────────────────────
    # Synthetic name; on-ledger type stays OfferCreate. Real-type updater
    # tracks state; ws_listener fires tx_result for these buckets.
    (
        "OfferCreateMPT",
        "/offer/create/mpt/random",
        offer_create_mpt,
        lambda w: (w.accounts, w.mpt_issuances, w.client),
        None,
    ),
    # PaymentMPT: synthetic name; on-ledger type is Payment (no updater).
    # ws_listener fires tx_result when a validated Payment carries an MPT leg.
    (
        "PaymentMPT",
        "/payment/mpt/random",
        payment_mpt,
        lambda w: (w.accounts, w.mpt_issuances, w.client),
        None,
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
        "DepositPreauth",
        "/deposit_preauth/random",
        deposit_preauth,
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
        lambda w: (w.accounts, w.protected_accounts, w.sponsored_accounts, w.client),
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
    *_SPONSOR_REGISTRY_ENTRIES,
]

TX_TYPES = [name for name, *_ in REGISTRY]
STATE_UPDATERS = {name: updater for name, _, _, _, updater in REGISTRY if updater}

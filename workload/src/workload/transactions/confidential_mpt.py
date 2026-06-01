"""Confidential MPT transaction generators for the antithesis workload (XLS-0096).

Phase 1: Faulty handlers for all 5 types + MergeInbox valid handler.
Phase 2: Valid handlers with real ZK proofs and ElGamal encryption.

Submits via raw JSON-RPC because xrpl-py v4.5.0 lacks Confidential MPT
models and binary codec definitions. When xrpl-py ships the models,
swap to ``submit_tx()`` and delete the raw submission helpers.
"""

from __future__ import annotations

from xrpl.asyncio.clients import AsyncJsonRpcClient
from xrpl.constants import XRPLException
from xrpl.models.requests import GenericRequest
from xrpl.wallet import Wallet

import workload.confidential_crypto as cc
from workload import params
from workload.assertions import tx_submitted
from workload.models import ConfidentialMPTIssuance, MPTokenIssuance, UserAccount
from workload.randoms import choice, randint

# ── Pending Send amount tracker ──────────────────────────────────────
# ConfidentialMPTSend has no plaintext amount in the validated tx, so the
# state updater can't know how much was sent.  We stash it here at
# submission time, keyed by (account, sequence, mpt_id), and the updater
# pops it to keep local spending_balance accurate.
_pending_send_amounts: dict[tuple[str, int, str], tuple[int, str]] = {}

# ── Raw submission helpers ───────────────────────────────────────────
# Bypasses xrpl-py serialization (which doesn't know Confidential MPT
# types yet). Rippled's ``submit`` RPC with ``tx_json`` + ``secret``
# handles server-side signing and autofill.


class _RawTx:
    """Minimal wrapper so ``tx_submitted()`` can extract object IDs."""

    def __init__(self, tx_json: dict) -> None:
        self._tx_json = tx_json

    def to_xrpl(self) -> dict:
        return self._tx_json


async def _submit_raw(
    name: str,
    tx_json: dict,
    wallet: Wallet,
    client: AsyncJsonRpcClient,
) -> dict:
    """Sign, autofill, and submit a raw transaction dict via JSON-RPC.

    Catches ``XRPLException`` so that overflow-amount mutations (values
    exceeding rippled's int64 range) still emit a ``tx_submitted`` event
    instead of bubbling up to the endpoint-level catch.
    """
    request = GenericRequest(
        method="submit",
        tx_json=tx_json,
        secret=wallet.seed,
    )
    try:
        response = await client.request(request)
    except XRPLException:
        # Overflow amounts or other parse failures — still record the
        # submission so the ``seen`` assertion fires.
        tx_submitted(name, _RawTx(tx_json), {})
        return {}
    result = response.result
    tx_submitted(name, _RawTx(tx_json), result)
    return result


# ── MergeInbox ───────────────────────────────────────────────────────


async def conf_mpt_merge_inbox(
    accounts: dict[str, UserAccount],
    mpt_issuances: list[MPTokenIssuance],
    conf_issuances: list[ConfidentialMPTIssuance],
    client: AsyncJsonRpcClient,
) -> None:
    if not cc.CRYPTO_AVAILABLE:
        return
    if params.should_send_faulty():
        return await _merge_inbox_faulty(accounts, mpt_issuances, client)
    return await _merge_inbox_valid(accounts, conf_issuances, client)


async def _merge_inbox_valid(
    accounts: dict[str, UserAccount],
    conf_issuances: list[ConfidentialMPTIssuance],
    client: AsyncJsonRpcClient,
) -> None:
    """MergeInbox moves inbox balance to spending balance."""
    if not conf_issuances:
        return
    ci = choice(conf_issuances)
    if not ci.holders:
        return
    holder_addr = choice(list(ci.holders.keys()))
    holder_acc = accounts.get(holder_addr)
    if not holder_acc:
        return
    tx_json = {
        "TransactionType": "ConfidentialMPTMergeInbox",
        "Account": holder_acc.address,
        "MPTokenIssuanceID": ci.mpt_issuance_id,
    }
    await _submit_raw("ConfidentialMPTMergeInbox", tx_json, holder_acc.wallet, client)


async def _merge_inbox_faulty(
    accounts: dict[str, UserAccount],
    mpt_issuances: list[MPTokenIssuance],
    client: AsyncJsonRpcClient,
) -> None:
    if not accounts:
        return
    src = choice(list(accounts.values()))
    mutation = choice(
        [
            "fake_mpt_id",
            "non_holder",
            "issuer_merge",
            "invalid_flags",
            "non_owner_submission",
        ]
    )

    if mutation == "fake_mpt_id":
        fake = params.fake_id()
        tx_json = {
            "TransactionType": "ConfidentialMPTMergeInbox",
            "Account": src.address,
            "MPTokenIssuanceID": fake,
        }
    elif mutation == "non_holder":
        # Use a real issuance but an account that likely hasn't opted in
        if not mpt_issuances:
            return
        mpt = choice(mpt_issuances)
        non_holders = [a for a in accounts.values() if a.address != mpt.issuer]
        if not non_holders:
            return
        impostor = choice(non_holders)
        tx_json = {
            "TransactionType": "ConfidentialMPTMergeInbox",
            "Account": impostor.address,
            "MPTokenIssuanceID": mpt.mpt_issuance_id,
        }
        src = impostor
    elif mutation == "issuer_merge":
        if not mpt_issuances:
            return
        mpt = choice(mpt_issuances)
        issuer = accounts.get(mpt.issuer)
        if not issuer:
            return
        tx_json = {
            "TransactionType": "ConfidentialMPTMergeInbox",
            "Account": issuer.address,
            "MPTokenIssuanceID": mpt.mpt_issuance_id,
        }
        src = issuer

    elif mutation == "invalid_flags":
        if not mpt_issuances:
            return
        mpt = choice(mpt_issuances)
        tx_json = {
            "TransactionType": "ConfidentialMPTMergeInbox",
            "Account": src.address,
            "MPTokenIssuanceID": mpt.mpt_issuance_id,
            "Flags": (_flg := params.confidential_invalid_flags()),
        }

    else:  # non_owner_submission
        if len(accounts) < 2:
            return
        if not mpt_issuances:
            return
        mpt = choice(mpt_issuances)
        impostor = choice([a for a in accounts.values() if a.address != src.address])
        tx_json = {
            "TransactionType": "ConfidentialMPTMergeInbox",
            "Account": src.address,
            "MPTokenIssuanceID": mpt.mpt_issuance_id,
        }
        # Sign with impostor's wallet → tefBAD_AUTH
        await _submit_raw("ConfidentialMPTMergeInbox", tx_json, impostor.wallet, client)
        return

    await _submit_raw("ConfidentialMPTMergeInbox", tx_json, src.wallet, client)


# ── Convert (public → confidential) ─────────────────────────────────


async def conf_mpt_convert(
    accounts: dict[str, UserAccount],
    mpt_issuances: list[MPTokenIssuance],
    conf_issuances: list[ConfidentialMPTIssuance],
    client: AsyncJsonRpcClient,
) -> None:
    if not cc.CRYPTO_AVAILABLE:
        return
    if params.should_send_faulty():
        return await _convert_faulty(accounts, mpt_issuances, client)
    return await _convert_valid(accounts, conf_issuances, client)


async def _convert_valid(
    accounts: dict[str, UserAccount],
    conf_issuances: list[ConfidentialMPTIssuance],
    client: AsyncJsonRpcClient,
) -> None:
    """Convert public MPT balance to confidential with real crypto.

    The first Convert for a holder (done during setup) sets the
    ``HolderEncryptionKey`` and includes a ``ZKProof`` (PoK of the
    private key).  Subsequent Converts must omit both — including them
    again causes rippled to reject the transaction.

    We check the ledger to determine whether the key is already
    registered, matching Ram's automation pattern.
    """
    if not conf_issuances:
        return
    ci = choice(conf_issuances)
    if not ci.holders:
        return
    holder_addr = choice(list(ci.holders.keys()))
    holder_acc = accounts.get(holder_addr)
    if not holder_acc or not holder_acc.elgamal_public_key:
        return

    amount = params.confidential_mpt_amount()
    bf = cc.generate_blinding_factor()

    # Encrypt amount for holder and issuer
    holder_ct = cc.encrypt(holder_acc.elgamal_public_key, amount, bf)
    issuer_ct = cc.encrypt(ci.issuer_pubkey, amount, bf)

    tx_json: dict = {
        "TransactionType": "ConfidentialMPTConvert",
        "Account": holder_acc.address,
        "MPTokenIssuanceID": ci.mpt_issuance_id,
        "MPTAmount": amount,
        "HolderEncryptedAmount": holder_ct,
        "IssuerEncryptedAmount": issuer_ct,
        "BlindingFactor": bf,
    }

    # ── Key registration: only on first Convert ──────────────────────
    # Check ledger to see if HolderEncryptionKey is already set.
    key_already_set = False
    try:
        resp = await client.request(
            GenericRequest(
                method="ledger_entry",
                mptoken={"mpt_issuance_id": ci.mpt_issuance_id, "account": holder_acc.address},
            )
        )
        node = resp.result.get("node", {})
        existing_key = node.get("HolderEncryptionKey", "")
        if existing_key:
            key_already_set = True
    except Exception:
        # MPToken doesn't exist yet → this IS the first convert
        pass

    if not key_already_set:
        # First convert — include key + PoK
        try:
            from xrpl.asyncio.account import get_next_valid_seq_number

            holder_seq = await get_next_valid_seq_number(holder_acc.address, client)
        except Exception:
            return
        holder_hex = cc.account_to_hex(holder_acc.address)
        ctx = cc.convert_context_hash(holder_hex, holder_seq, ci.mpt_issuance_id)
        pok = cc.pok_proof(holder_acc.elgamal_private_key, holder_acc.elgamal_public_key, ctx)
        tx_json["HolderEncryptionKey"] = holder_acc.elgamal_public_key
        tx_json["ZKProof"] = pok
        tx_json["Sequence"] = holder_seq
    else:
        pass  # Subsequent convert: key already registered, no PoK needed

    await _submit_raw("ConfidentialMPTConvert", tx_json, holder_acc.wallet, client)


def _base_convert_tx(account: str, mpt_issuance_id: str) -> dict:
    """Build a Convert tx with all required crypto fields (garbage)."""
    return {
        "TransactionType": "ConfidentialMPTConvert",
        "Account": account,
        "MPTokenIssuanceID": mpt_issuance_id,
        "MPTAmount": params.confidential_mpt_amount(),
        "HolderEncryptedAmount": params.confidential_ciphertext(),
        "IssuerEncryptedAmount": params.confidential_ciphertext(),
        "BlindingFactor": params.confidential_blinding_factor(),
    }


async def _convert_faulty(
    accounts: dict[str, UserAccount],
    mpt_issuances: list[MPTokenIssuance],
    client: AsyncJsonRpcClient,
) -> None:
    if not accounts:
        return
    src = choice(list(accounts.values()))
    mpt_id = choice(mpt_issuances).mpt_issuance_id if mpt_issuances else params.fake_id()

    mutation = choice(
        [
            "wrong_length_ciphertext",
            "truncated_ciphertext",
            "wrong_length_blinding",
            "empty_blinding",
            "garbage_proof_with_key",
            "wrong_length_proof",
            "wrong_length_key",
            "key_without_proof",
            "proof_without_key",
            "fake_mpt_id",
            "zero_amount",
            "negative_amount",
            "overflow_amount",
            "overdraw_amount",
            "invalid_flags",
            "all_zero_proof",
            "swapped_fields",
            "point_not_on_curve",
            "non_owner_submission",
        ]
    )

    tx_json = _base_convert_tx(src.address, mpt_id)

    if mutation == "wrong_length_ciphertext":
        tx_json["HolderEncryptedAmount"] = params.confidential_wrong_length_hex(
            params._CIPHERTEXT_HEX_LEN
        )

    elif mutation == "truncated_ciphertext":
        tx_json["HolderEncryptedAmount"] = params.confidential_hex(params._CIPHERTEXT_HEX_LEN // 2)

    elif mutation == "empty_blinding":
        tx_json["BlindingFactor"] = ""

    elif mutation == "wrong_length_blinding":
        tx_json["BlindingFactor"] = params.confidential_wrong_length_hex(
            params._BLINDING_FACTOR_HEX_LEN
        )

    elif mutation == "garbage_proof_with_key":
        tx_json["HolderEncryptionKey"] = params.confidential_encryption_key()
        tx_json["ZKProof"] = params.confidential_schnorr_proof()

    elif mutation == "wrong_length_proof":
        tx_json["HolderEncryptionKey"] = params.confidential_encryption_key()
        tx_json["ZKProof"] = params.confidential_wrong_length_hex(params._SCHNORR_PROOF_HEX_LEN)

    elif mutation == "wrong_length_key":
        tx_json["HolderEncryptionKey"] = params.confidential_wrong_length_hex(
            params._ENCRYPTION_KEY_HEX_LEN
        )
        tx_json["ZKProof"] = params.confidential_schnorr_proof()

    elif mutation == "key_without_proof":
        tx_json["HolderEncryptionKey"] = params.confidential_encryption_key()

    elif mutation == "proof_without_key":
        tx_json["ZKProof"] = params.confidential_schnorr_proof()

    elif mutation == "fake_mpt_id":
        tx_json["MPTokenIssuanceID"] = params.fake_id()

    elif mutation == "zero_amount":
        tx_json["MPTAmount"] = 0

    elif mutation == "negative_amount":
        tx_json["MPTAmount"] = (_neg := params.confidential_negative_amount())

    elif mutation == "overflow_amount":
        tx_json["MPTAmount"] = (_ovf := params.confidential_overflow_amount())

    elif mutation == "overdraw_amount":
        amt = params.confidential_mpt_amount() + randint(1_000_000, 9_999_999)
        tx_json["MPTAmount"] = amt

    elif mutation == "invalid_flags":
        tx_json["Flags"] = (_flg := params.confidential_invalid_flags())

    elif mutation == "all_zero_proof":
        tx_json["HolderEncryptionKey"] = params.confidential_encryption_key()
        tx_json["ZKProof"] = params.confidential_zero_hex(params._SCHNORR_PROOF_HEX_LEN)

    elif mutation == "swapped_fields":
        ct = tx_json["HolderEncryptedAmount"]
        bf = tx_json["BlindingFactor"]
        tx_json["HolderEncryptedAmount"] = bf  # wrong length for ciphertext
        tx_json["BlindingFactor"] = ct  # wrong length for blinding factor

    elif mutation == "point_not_on_curve":
        tx_json["HolderEncryptionKey"] = params.confidential_not_on_curve_point()
        tx_json["ZKProof"] = params.confidential_schnorr_proof()

    else:  # non_owner_submission
        if len(accounts) < 2:
            return
        impostor = choice([a for a in accounts.values() if a.address != src.address])
        await _submit_raw("ConfidentialMPTConvert", tx_json, impostor.wallet, client)
        return

    await _submit_raw("ConfidentialMPTConvert", tx_json, src.wallet, client)


# ── Send (confidential → confidential) ──────────────────────────────


async def conf_mpt_send(
    accounts: dict[str, UserAccount],
    mpt_issuances: list[MPTokenIssuance],
    conf_issuances: list[ConfidentialMPTIssuance],
    client: AsyncJsonRpcClient,
) -> None:
    if not cc.CRYPTO_AVAILABLE:
        return
    if params.should_send_faulty():
        return await _send_faulty(accounts, mpt_issuances, client)
    return await _send_valid(accounts, conf_issuances, client)


async def _send_valid(
    accounts: dict[str, UserAccount],
    conf_issuances: list[ConfidentialMPTIssuance],
    client: AsyncJsonRpcClient,
) -> None:
    """Confidential → confidential transfer with real ZK proofs."""
    if not conf_issuances:
        return
    ci = choice(conf_issuances)
    # Need at least 2 holders — use local state for cheap precondition only
    candidates = [
        addr for addr in ci.holders if addr in accounts and accounts[addr].elgamal_public_key
    ]
    if len(candidates) < 2:
        return

    sender_addr = choice(candidates)
    dest_addr = choice([a for a in candidates if a != sender_addr])
    sender_acc = accounts[sender_addr]
    dest_acc = accounts[dest_addr]
    if ci.issuer not in accounts:
        return

    # ── Ledger truth: fetch + decrypt actual balance ──────────────────
    try:
        resp = await client.request(
            GenericRequest(
                method="ledger_entry",
                mptoken={"mpt_issuance_id": ci.mpt_issuance_id, "account": sender_acc.address},
            )
        )
        node = resp.result.get("node", {})
        prev_bal_ct = node.get("ConfidentialBalanceSpending", "")
        if not prev_bal_ct:
            return
        current_balance = cc.decrypt(sender_acc.elgamal_private_key, prev_bal_ct)
        version = node.get("ConfidentialBalanceVersion", 0)
    except Exception:
        return
    if current_balance <= 0:
        return

    # Send a fraction of the actual ledger balance
    amount = max(1, current_balance // randint(2, 10))

    # Shared blinding factor for all recipients' ElGamal encryption
    shared_bf = cc.generate_blinding_factor()

    # Encrypt amount for all participants using shared BF
    sender_ct = cc.encrypt(sender_acc.elgamal_public_key, amount, shared_bf)
    dest_ct = cc.encrypt(dest_acc.elgamal_public_key, amount, shared_bf)
    issuer_ct = cc.encrypt(ci.issuer_pubkey, amount, shared_bf)

    # Pedersen commitments
    amt_bf = shared_bf  # amount commitment BF must match shared encryption BF
    amt_comm = cc.pedersen_commitment(amount, amt_bf)
    bal_bf = cc.generate_blinding_factor()
    bal_comm = cc.pedersen_commitment(current_balance, bal_bf)

    # Context hash and participants list
    try:
        from xrpl.asyncio.account import get_next_valid_seq_number

        sender_seq = await get_next_valid_seq_number(sender_acc.address, client)
    except Exception:
        return
    sender_hex = cc.account_to_hex(sender_acc.address)
    dest_hex = cc.account_to_hex(dest_acc.address)
    ctx = cc.send_context_hash(sender_hex, sender_seq, ci.mpt_issuance_id, dest_hex, version)

    # Build participants list: [(pubkey, ciphertext), ...]
    participants = [
        (sender_acc.elgamal_public_key, sender_ct),
        (dest_acc.elgamal_public_key, dest_ct),
        (ci.issuer_pubkey, issuer_ct),
    ]

    zk_proof = cc.send_proof(
        sender_privkey=sender_acc.elgamal_private_key,
        sender_pubkey=sender_acc.elgamal_public_key,
        amount=amount,
        current_balance=current_balance,
        participants=participants,
        tx_blinding_factor=shared_bf,
        context_hash=ctx,
        amount_commitment=amt_comm,
        balance_commitment=bal_comm,
        balance_blinding=bal_bf,
        sender_balance_encrypted=prev_bal_ct,
    )

    tx_json = {
        "TransactionType": "ConfidentialMPTSend",
        "Account": sender_acc.address,
        "Destination": dest_acc.address,
        "MPTokenIssuanceID": ci.mpt_issuance_id,
        "SenderEncryptedAmount": sender_ct,
        "DestinationEncryptedAmount": dest_ct,
        "IssuerEncryptedAmount": issuer_ct,
        "AmountCommitment": amt_comm,
        "BalanceCommitment": bal_comm,
        "ZKProof": zk_proof,
        "Sequence": sender_seq,
    }
    result = await _submit_raw("ConfidentialMPTSend", tx_json, sender_acc.wallet, client)
    # Stash amount for the state updater — keyed by (account, seq, mpt_id)
    # so _on_conf_send can update spending_balance accurately.
    if result:
        _pending_send_amounts[(sender_acc.address, sender_seq, ci.mpt_issuance_id)] = (
            amount,
            dest_acc.address,
        )


def _base_send_tx(sender: str, destination: str, mpt_issuance_id: str) -> dict:
    """Build a Send tx with all required crypto fields (garbage)."""
    return {
        "TransactionType": "ConfidentialMPTSend",
        "Account": sender,
        "Destination": destination,
        "MPTokenIssuanceID": mpt_issuance_id,
        "SenderEncryptedAmount": params.confidential_ciphertext(),
        "DestinationEncryptedAmount": params.confidential_ciphertext(),
        "IssuerEncryptedAmount": params.confidential_ciphertext(),
        "AmountCommitment": params.confidential_commitment(),
        "BalanceCommitment": params.confidential_commitment(),
        "ZKProof": params.confidential_send_proof(),
    }


async def _send_faulty(
    accounts: dict[str, UserAccount],
    mpt_issuances: list[MPTokenIssuance],
    client: AsyncJsonRpcClient,
) -> None:
    if not accounts or len(accounts) < 2:
        return
    acct_list = list(accounts.values())
    src = choice(acct_list)
    dst = choice([a for a in acct_list if a.address != src.address])
    mpt_id = choice(mpt_issuances).mpt_issuance_id if mpt_issuances else params.fake_id()

    mutation = choice(
        [
            "truncated_proof",
            "wrong_length_proof",
            "mismatched_ciphertexts",
            "wrong_length_ciphertext",
            "empty_commitment",
            "wrong_length_commitment",
            "self_send",
            "fake_mpt_id",
            "non_participant",
            "invalid_flags",
            "all_zero_proof",
            "swapped_fields",
            "send_to_issuer",
            "non_owner_submission",
        ]
    )

    tx_json = _base_send_tx(src.address, dst.address, mpt_id)

    if mutation == "truncated_proof":
        tx_json["ZKProof"] = params.confidential_hex(params._SEND_PROOF_HEX_LEN // 2)

    elif mutation == "wrong_length_proof":
        tx_json["ZKProof"] = params.confidential_wrong_length_hex(params._SEND_PROOF_HEX_LEN)

    elif mutation == "mismatched_ciphertexts":
        same_ct = params.confidential_ciphertext()
        tx_json["SenderEncryptedAmount"] = same_ct
        tx_json["DestinationEncryptedAmount"] = same_ct

    elif mutation == "wrong_length_ciphertext":
        tx_json["SenderEncryptedAmount"] = params.confidential_wrong_length_hex(
            params._CIPHERTEXT_HEX_LEN
        )

    elif mutation == "empty_commitment":
        tx_json["AmountCommitment"] = ""

    elif mutation == "wrong_length_commitment":
        tx_json["AmountCommitment"] = params.confidential_wrong_length_hex(
            params._COMMITMENT_HEX_LEN
        )

    elif mutation == "self_send":
        tx_json["Destination"] = src.address

    elif mutation == "fake_mpt_id":
        tx_json["MPTokenIssuanceID"] = params.fake_id()

    elif mutation == "non_participant":
        random_dest = choice(acct_list).address
        tx_json["Destination"] = random_dest

    elif mutation == "invalid_flags":
        tx_json["Flags"] = (_flg := params.confidential_invalid_flags())

    elif mutation == "all_zero_proof":
        tx_json["ZKProof"] = params.confidential_zero_hex(params._SEND_PROOF_HEX_LEN)

    elif mutation == "swapped_fields":
        ct = tx_json["SenderEncryptedAmount"]
        cm = tx_json["AmountCommitment"]
        tx_json["SenderEncryptedAmount"] = cm  # 66 hex where 132 expected
        tx_json["AmountCommitment"] = ct  # 132 hex where 66 expected

    elif mutation == "send_to_issuer":
        if mpt_issuances:
            mpt = choice(mpt_issuances)
            tx_json["MPTokenIssuanceID"] = mpt.mpt_issuance_id
            issuer = accounts.get(mpt.issuer)
            if issuer:
                tx_json["Destination"] = issuer.address

    else:  # non_owner_submission
        impostor = choice([a for a in acct_list if a.address != src.address])
        await _submit_raw("ConfidentialMPTSend", tx_json, impostor.wallet, client)
        return

    await _submit_raw("ConfidentialMPTSend", tx_json, src.wallet, client)


# ── ConvertBack (confidential → public) ─────────────────────────────


async def conf_mpt_convert_back(
    accounts: dict[str, UserAccount],
    mpt_issuances: list[MPTokenIssuance],
    conf_issuances: list[ConfidentialMPTIssuance],
    client: AsyncJsonRpcClient,
) -> None:
    if not cc.CRYPTO_AVAILABLE:
        return
    if params.should_send_faulty():
        return await _convert_back_faulty(accounts, mpt_issuances, client)
    return await _convert_back_valid(accounts, conf_issuances, client)


async def _convert_back_valid(
    accounts: dict[str, UserAccount],
    conf_issuances: list[ConfidentialMPTIssuance],
    client: AsyncJsonRpcClient,
) -> None:
    """Convert confidential balance back to public with real ZK proof."""
    if not conf_issuances:
        return
    ci = choice(conf_issuances)
    # Cheap precondition — pick a holder that we *think* has balance
    candidates = [
        addr for addr in ci.holders if addr in accounts and accounts[addr].elgamal_public_key
    ]
    if not candidates:
        return

    holder_addr = choice(candidates)
    holder_acc = accounts[holder_addr]

    # ── Ledger truth: fetch + decrypt actual balance ──────────────────
    try:
        resp = await client.request(
            GenericRequest(
                method="ledger_entry",
                mptoken={"mpt_issuance_id": ci.mpt_issuance_id, "account": holder_acc.address},
            )
        )
        node = resp.result.get("node", {})
        prev_bal_ct = node.get("ConfidentialBalanceSpending", "")
        if not prev_bal_ct:
            return
        current_balance = cc.decrypt(holder_acc.elgamal_private_key, prev_bal_ct)
        version = node.get("ConfidentialBalanceVersion", 0)
    except Exception:
        return
    if current_balance <= 0:
        return

    amount = max(1, current_balance // randint(2, 10))
    bf = cc.generate_blinding_factor()

    # Encrypt amount for holder and issuer
    holder_ct = cc.encrypt(holder_acc.elgamal_public_key, amount, bf)
    issuer_ct = cc.encrypt(ci.issuer_pubkey, amount, bf)

    # Balance commitment
    bal_bf = cc.generate_blinding_factor()
    bal_comm = cc.pedersen_commitment(current_balance, bal_bf)

    # Context hash
    try:
        from xrpl.asyncio.account import get_next_valid_seq_number

        holder_seq = await get_next_valid_seq_number(holder_acc.address, client)
    except Exception:
        return
    holder_hex = cc.account_to_hex(holder_acc.address)
    ctx = cc.convert_back_context_hash(holder_hex, holder_seq, ci.mpt_issuance_id, version)

    zk_proof = cc.convert_back_proof(
        holder_privkey=holder_acc.elgamal_private_key,
        holder_pubkey=holder_acc.elgamal_public_key,
        amount=amount,
        current_balance=current_balance,
        context_hash=ctx,
        balance_commitment=bal_comm,
        balance_blinding=bal_bf,
        holder_balance_encrypted=prev_bal_ct,
    )

    tx_json = {
        "TransactionType": "ConfidentialMPTConvertBack",
        "Account": holder_acc.address,
        "MPTokenIssuanceID": ci.mpt_issuance_id,
        "MPTAmount": amount,
        "HolderEncryptedAmount": holder_ct,
        "IssuerEncryptedAmount": issuer_ct,
        "BlindingFactor": bf,
        "BalanceCommitment": bal_comm,
        "ZKProof": zk_proof,
        "Sequence": holder_seq,
    }
    await _submit_raw("ConfidentialMPTConvertBack", tx_json, holder_acc.wallet, client)


def _base_convert_back_tx(account: str, mpt_issuance_id: str) -> dict:
    """Build a ConvertBack tx with all required crypto fields (garbage)."""
    return {
        "TransactionType": "ConfidentialMPTConvertBack",
        "Account": account,
        "MPTokenIssuanceID": mpt_issuance_id,
        "MPTAmount": params.confidential_mpt_amount(),
        "HolderEncryptedAmount": params.confidential_ciphertext(),
        "IssuerEncryptedAmount": params.confidential_ciphertext(),
        "BlindingFactor": params.confidential_blinding_factor(),
        "BalanceCommitment": params.confidential_commitment(),
        "ZKProof": params.confidential_convert_back_proof(),
    }


async def _convert_back_faulty(
    accounts: dict[str, UserAccount],
    mpt_issuances: list[MPTokenIssuance],
    client: AsyncJsonRpcClient,
) -> None:
    if not accounts:
        return
    src = choice(list(accounts.values()))
    mpt_id = choice(mpt_issuances).mpt_issuance_id if mpt_issuances else params.fake_id()

    mutation = choice(
        [
            "truncated_proof",
            "wrong_length_proof",
            "mismatched_ciphertexts",
            "wrong_length_ciphertext",
            "empty_commitment",
            "wrong_length_commitment",
            "fake_mpt_id",
            "overdraw_amount",
            "negative_amount",
            "overflow_amount",
            "invalid_flags",
            "all_zero_proof",
            "swapped_fields",
            "non_owner_submission",
        ]
    )

    tx_json = _base_convert_back_tx(src.address, mpt_id)

    if mutation == "truncated_proof":
        tx_json["ZKProof"] = params.confidential_hex(params._CONVERT_BACK_PROOF_HEX_LEN // 2)

    elif mutation == "wrong_length_proof":
        tx_json["ZKProof"] = params.confidential_wrong_length_hex(
            params._CONVERT_BACK_PROOF_HEX_LEN
        )

    elif mutation == "mismatched_ciphertexts":
        same_ct = params.confidential_ciphertext()
        tx_json["HolderEncryptedAmount"] = same_ct
        tx_json["IssuerEncryptedAmount"] = same_ct

    elif mutation == "wrong_length_ciphertext":
        tx_json["HolderEncryptedAmount"] = params.confidential_wrong_length_hex(
            params._CIPHERTEXT_HEX_LEN
        )

    elif mutation == "empty_commitment":
        tx_json["BalanceCommitment"] = ""

    elif mutation == "wrong_length_commitment":
        tx_json["BalanceCommitment"] = params.confidential_wrong_length_hex(
            params._COMMITMENT_HEX_LEN
        )

    elif mutation == "fake_mpt_id":
        tx_json["MPTokenIssuanceID"] = params.fake_id()

    elif mutation == "overdraw_amount":
        amt = params.confidential_mpt_amount() + randint(1_000_000, 9_999_999)
        tx_json["MPTAmount"] = amt

    elif mutation == "negative_amount":
        tx_json["MPTAmount"] = (_neg := params.confidential_negative_amount())

    elif mutation == "overflow_amount":
        tx_json["MPTAmount"] = (_ovf := params.confidential_overflow_amount())

    elif mutation == "invalid_flags":
        tx_json["Flags"] = (_flg := params.confidential_invalid_flags())

    elif mutation == "all_zero_proof":
        tx_json["ZKProof"] = params.confidential_zero_hex(params._CONVERT_BACK_PROOF_HEX_LEN)

    elif mutation == "swapped_fields":
        ct = tx_json["HolderEncryptedAmount"]
        cm = tx_json["BalanceCommitment"]
        tx_json["HolderEncryptedAmount"] = cm
        tx_json["BalanceCommitment"] = ct

    else:  # non_owner_submission
        if len(accounts) < 2:
            return
        impostor = choice([a for a in accounts.values() if a.address != src.address])
        await _submit_raw("ConfidentialMPTConvertBack", tx_json, impostor.wallet, client)
        return

    await _submit_raw("ConfidentialMPTConvertBack", tx_json, src.wallet, client)


# ── Clawback (issuer reclaims confidential balance) ─────────────────


async def conf_mpt_clawback(
    accounts: dict[str, UserAccount],
    mpt_issuances: list[MPTokenIssuance],
    conf_issuances: list[ConfidentialMPTIssuance],
    client: AsyncJsonRpcClient,
) -> None:
    if not cc.CRYPTO_AVAILABLE:
        return
    if params.should_send_faulty():
        return await _clawback_faulty(accounts, mpt_issuances, client)
    return await _clawback_valid(accounts, conf_issuances, client)


async def _clawback_valid(
    accounts: dict[str, UserAccount],
    conf_issuances: list[ConfidentialMPTIssuance],
    client: AsyncJsonRpcClient,
) -> None:
    """Issuer clawback of confidential balance with real ZK proof."""
    if not conf_issuances:
        return
    ci = choice(conf_issuances)
    if not ci.holders:
        return
    issuer_acc = accounts.get(ci.issuer)
    if not issuer_acc or not issuer_acc.elgamal_public_key:
        return

    holder_addr = choice(list(ci.holders.keys()))

    # ── Ledger truth: fetch + decrypt via issuer key ──────────────────
    try:
        resp = await client.request(
            GenericRequest(
                method="ledger_entry",
                mptoken={"mpt_issuance_id": ci.mpt_issuance_id, "account": holder_addr},
            )
        )
        issuer_encrypted_bal = resp.result.get("node", {}).get("IssuerEncryptedBalance", "")
        if not issuer_encrypted_bal:
            return
        holder_balance = cc.decrypt(ci.issuer_privkey, issuer_encrypted_bal)
    except Exception:
        return
    if holder_balance <= 0:
        return

    # XLS-0096: Clawback burns the holder's ENTIRE confidential balance.
    amount = holder_balance

    # Context hash
    try:
        from xrpl.asyncio.account import get_next_valid_seq_number

        issuer_seq = await get_next_valid_seq_number(issuer_acc.address, client)
    except Exception:
        return

    issuer_hex = cc.account_to_hex(issuer_acc.address)
    holder_hex = cc.account_to_hex(holder_addr)
    ctx = cc.clawback_context_hash(issuer_hex, issuer_seq, ci.mpt_issuance_id, holder_hex)

    zk_proof = cc.clawback_proof(
        issuer_privkey=ci.issuer_privkey,
        issuer_pubkey=ci.issuer_pubkey,
        amount=amount,
        context_hash=ctx,
        issuer_encrypted_balance=issuer_encrypted_bal,
    )

    tx_json = {
        "TransactionType": "ConfidentialMPTClawback",
        "Account": issuer_acc.address,
        "MPTokenIssuanceID": ci.mpt_issuance_id,
        "Holder": holder_addr,
        "MPTAmount": amount,
        "ZKProof": zk_proof,
        "Sequence": issuer_seq,
    }
    await _submit_raw("ConfidentialMPTClawback", tx_json, issuer_acc.wallet, client)


def _base_clawback_tx(issuer: str, holder: str, mpt_issuance_id: str) -> dict:
    """Build a Clawback tx with all required crypto fields (garbage)."""
    return {
        "TransactionType": "ConfidentialMPTClawback",
        "Account": issuer,
        "Holder": holder,
        "MPTokenIssuanceID": mpt_issuance_id,
        "MPTAmount": params.confidential_mpt_amount(),
        "ZKProof": params.confidential_schnorr_proof(),
    }


async def _clawback_faulty(
    accounts: dict[str, UserAccount],
    mpt_issuances: list[MPTokenIssuance],
    client: AsyncJsonRpcClient,
) -> None:
    if not accounts or len(accounts) < 2:
        return
    acct_list = list(accounts.values())
    src = choice(acct_list)
    holder = choice([a for a in acct_list if a.address != src.address])
    mpt_id = choice(mpt_issuances).mpt_issuance_id if mpt_issuances else params.fake_id()

    mutation = choice(
        [
            "truncated_proof",
            "wrong_length_proof",
            "non_issuer",
            "fake_mpt_id",
            "self_clawback",
            "overdraw_amount",
            "negative_amount",
            "overflow_amount",
            "invalid_flags",
            "all_zero_proof",
            "non_owner_submission",
        ]
    )

    tx_json = _base_clawback_tx(src.address, holder.address, mpt_id)

    if mutation == "truncated_proof":
        tx_json["ZKProof"] = params.confidential_hex(params._SCHNORR_PROOF_HEX_LEN // 2)

    elif mutation == "wrong_length_proof":
        tx_json["ZKProof"] = params.confidential_wrong_length_hex(params._SCHNORR_PROOF_HEX_LEN)

    elif mutation == "non_issuer":
        if mpt_issuances:
            mpt = choice(mpt_issuances)
            non_issuers = [a for a in acct_list if a.address != mpt.issuer]
            if non_issuers:
                impostor = choice(non_issuers)
                tx_json["Account"] = impostor.address
                tx_json["MPTokenIssuanceID"] = mpt.mpt_issuance_id
                src = impostor

    elif mutation == "fake_mpt_id":
        tx_json["MPTokenIssuanceID"] = params.fake_id()

    elif mutation == "self_clawback":
        tx_json["Holder"] = src.address

    elif mutation == "overdraw_amount":
        amt = params.confidential_mpt_amount() + randint(1_000_000, 9_999_999)
        tx_json["MPTAmount"] = amt

    elif mutation == "negative_amount":
        tx_json["MPTAmount"] = (_neg := params.confidential_negative_amount())

    elif mutation == "overflow_amount":
        tx_json["MPTAmount"] = (_ovf := params.confidential_overflow_amount())

    elif mutation == "invalid_flags":
        tx_json["Flags"] = (_flg := params.confidential_invalid_flags())

    elif mutation == "all_zero_proof":
        tx_json["ZKProof"] = params.confidential_zero_hex(params._SCHNORR_PROOF_HEX_LEN)

    else:  # non_owner_submission
        impostor = choice([a for a in acct_list if a.address != src.address])
        await _submit_raw("ConfidentialMPTClawback", tx_json, impostor.wallet, client)
        return

    await _submit_raw("ConfidentialMPTClawback", tx_json, src.wallet, client)

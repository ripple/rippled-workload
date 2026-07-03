"""Confidential MPT (XLS-0096) generators: MergeInbox, Convert, Send, ConvertBack, Clawback."""

from __future__ import annotations

from collections.abc import Callable

from xrpl.asyncio.clients import AsyncJsonRpcClient
from xrpl.models.transactions import (
    ConfidentialMPTClawback,
    ConfidentialMPTConvert,
    ConfidentialMPTConvertBack,
    ConfidentialMPTMergeInbox,
    ConfidentialMPTSend,
)

import workload.confidential_crypto as cc
from workload import params
from workload.fuzz import submit_fuzzed
from workload.models import ConfidentialMPTIssuance, MPTokenIssuance, UserAccount
from workload.randoms import choice, randint
from workload.submit import submit_raw, submit_tx

# ── Pending Send amount side-channel ─────────────────────────────────
# Send carries no plaintext amount (encrypted); stash it keyed by the proof-bound
# sequence so _on_conf_send can update local balances.
_pending_send_amounts: dict[tuple[str, int, str], tuple[int, str]] = {}


# ── MergeInbox ────────────────────────────────────────────────────────


async def conf_mpt_merge_inbox(
    accounts: dict[str, UserAccount],
    mpt_issuances: list[MPTokenIssuance],
    conf_issuances: list[ConfidentialMPTIssuance],
    client: AsyncJsonRpcClient,
) -> None:
    if params.should_send_faulty():
        return await _merge_inbox_faulty(accounts, mpt_issuances, client)
    return await _merge_inbox_valid(accounts, conf_issuances, client)


def _merge_inbox_base(account: str, mpt_id: str) -> ConfidentialMPTMergeInbox:
    return ConfidentialMPTMergeInbox(
        account=account,
        mptoken_issuance_id=mpt_id,
        fee=params.confidential_fee(),
    )


async def _merge_inbox_valid(
    accounts: dict[str, UserAccount],
    conf_issuances: list[ConfidentialMPTIssuance],
    client: AsyncJsonRpcClient,
) -> None:
    if not cc.CRYPTO_AVAILABLE or not conf_issuances:
        return
    ci = choice(conf_issuances)
    holders = [h for h in ci.holders if h in accounts]
    if not holders:
        return
    holder = accounts[choice(holders)]
    try:
        base = await cc.build_merge_inbox(client.url, holder.wallet, ci.mpt_issuance_id)
    except cc.BUILD_SKIP_ERRORS:
        return
    await submit_tx("ConfidentialMPTMergeInbox", base, client, holder.wallet)


async def _merge_inbox_faulty(
    accounts: dict[str, UserAccount],
    mpt_issuances: list[MPTokenIssuance],
    client: AsyncJsonRpcClient,
) -> None:
    if not accounts:
        return
    src = choice(list(accounts.values()))
    real_id = choice(mpt_issuances).mpt_issuance_id if mpt_issuances else params.fake_mpt_id()

    mutation = choice(["fake_mpt_id", "non_holder", "invalid_flags", "non_owner", "fuzz"])

    if mutation == "fuzz":
        base = _merge_inbox_base(src.address, real_id)
        await submit_fuzzed("ConfidentialMPTMergeInbox", base, client, src.wallet)
        return

    mutate: Callable[[dict], None] | None = None
    wallet = src.wallet

    if mutation == "fake_mpt_id":
        # -> tecOBJECT_NOT_FOUND; reliable validating-tec feeder.
        base = _merge_inbox_base(src.address, params.fake_mpt_id())
    elif mutation == "non_holder":
        # Account never opted in -> tecOBJECT_NOT_FOUND.
        base = _merge_inbox_base(src.address, real_id)
    elif mutation == "invalid_flags":
        # -> temINVALID_FLAG.
        base = _merge_inbox_base(src.address, real_id)
        flags = params.confidential_invalid_flags()

        def _set_flags(d: dict) -> None:
            d["Flags"] = flags

        mutate = _set_flags
    else:  # non_owner -> tefBAD_AUTH.
        if len(accounts) < 2:
            return
        impostor = choice([a for a in accounts.values() if a.address != src.address])
        base = _merge_inbox_base(src.address, real_id)
        wallet = impostor.wallet

    await submit_raw("ConfidentialMPTMergeInbox", base, client, wallet, mutate)


# ── Convert (public -> confidential) ─────────────────────────────────


async def conf_mpt_convert(
    accounts: dict[str, UserAccount],
    mpt_issuances: list[MPTokenIssuance],
    conf_issuances: list[ConfidentialMPTIssuance],
    client: AsyncJsonRpcClient,
) -> None:
    if params.should_send_faulty():
        return await _convert_faulty(accounts, mpt_issuances, client)
    return await _convert_valid(accounts, conf_issuances, client)


def _convert_base(account: str, mpt_id: str) -> ConfidentialMPTConvert:
    """Convert base whose trivial ciphertexts pass preflight to preclaim (key+proof omitted)."""
    return ConfidentialMPTConvert(
        account=account,
        mptoken_issuance_id=mpt_id,
        mpt_amount=params.confidential_mpt_amount(),
        holder_encrypted_amount=params.confidential_ciphertext(),
        issuer_encrypted_amount=params.confidential_ciphertext(),
        blinding_factor=params.confidential_blinding_factor(),
        fee=params.confidential_fee(),
    )


async def _convert_valid(
    accounts: dict[str, UserAccount],
    conf_issuances: list[ConfidentialMPTIssuance],
    client: AsyncJsonRpcClient,
) -> None:
    if not cc.CRYPTO_AVAILABLE or not conf_issuances:
        return
    ci = choice(conf_issuances)
    if not ci.issuer_pubkey:
        return
    holders = [h for h in ci.holders if h in accounts and accounts[h].elgamal_public_key]
    if not holders:
        return
    holder = accounts[choice(holders)]
    # Builder binds account Sequence into the proof; stamp same seq on submit (else tecBAD_PROOF).
    try:
        seq = await cc.account_sequence(client.url, holder.address)
        base = await cc.build_convert(
            client.url,
            holder.wallet,
            ci.mpt_issuance_id,
            params.confidential_mpt_amount(),
            ci.issuer_pubkey,
            holder.elgamal_private_key,
            holder.elgamal_public_key,
        )
    except cc.BUILD_SKIP_ERRORS:
        return
    await submit_tx("ConfidentialMPTConvert", base, client, holder.wallet, seq=seq)


async def _convert_faulty(
    accounts: dict[str, UserAccount],
    mpt_issuances: list[MPTokenIssuance],
    client: AsyncJsonRpcClient,
) -> None:
    if not accounts:
        return
    src = choice(list(accounts.values()))
    real_id = choice(mpt_issuances).mpt_issuance_id if mpt_issuances else params.fake_mpt_id()

    mutation = choice(
        [
            "garbage_proof_with_key",
            "wrong_length_ciphertext",
            "wrong_length_blinding",
            "key_without_proof",
            "point_not_on_curve",
            "zero_amount",
            "fake_mpt_id",
            "invalid_flags",
            "non_owner",
            "fuzz",
        ]
    )

    if mutation == "fuzz":
        base = _convert_base(src.address, real_id)
        await submit_fuzzed("ConfidentialMPTConvert", base, client, src.wallet)
        return

    base = _convert_base(src.address, real_id)
    wallet = src.wallet
    mutate: Callable[[dict], None] | None = None

    if mutation == "garbage_proof_with_key":
        # On-curve key + bogus proof -> preclaim tecBAD_PROOF/tecOBJECT_NOT_FOUND;
        # the reliable validating-tec failure feeder.
        key = params.confidential_encryption_key()
        proof = params.confidential_schnorr_proof()

        def _set_key_proof(d: dict) -> None:
            d["HolderEncryptionKey"] = key
            d["ZKProof"] = proof

        mutate = _set_key_proof
    elif mutation == "wrong_length_ciphertext":
        # -> temBAD_CIPHERTEXT.
        ct = params.confidential_wrong_length_hex(params._CIPHERTEXT_HEX_LEN)

        def _set_ct(d: dict) -> None:
            d["HolderEncryptedAmount"] = ct

        mutate = _set_ct
    elif mutation == "wrong_length_blinding":
        # -> temBAD_CIPHERTEXT.
        bf = params.confidential_wrong_length_hex(params._BLINDING_FACTOR_HEX_LEN)

        def _set_bf(d: dict) -> None:
            d["BlindingFactor"] = bf

        mutate = _set_bf
    elif mutation == "key_without_proof":
        # Key present, ZKProof absent -> temMALFORMED.
        key = params.confidential_encryption_key()

        def _set_key(d: dict) -> None:
            d["HolderEncryptionKey"] = key

        mutate = _set_key
    elif mutation == "point_not_on_curve":
        # Off-curve key -> temMALFORMED.
        key = params.confidential_not_on_curve_point()
        proof = params.confidential_schnorr_proof()

        def _set_offcurve(d: dict) -> None:
            d["HolderEncryptionKey"] = key
            d["ZKProof"] = proof

        mutate = _set_offcurve
    elif mutation == "zero_amount":
        # -> temBAD_AMOUNT.
        def _set_zero(d: dict) -> None:
            d["MPTAmount"] = "0"

        mutate = _set_zero
    elif mutation == "fake_mpt_id":
        # -> preclaim tecOBJECT_NOT_FOUND (validating-tec feeder).
        base = _convert_base(src.address, params.fake_mpt_id())
    elif mutation == "invalid_flags":
        # -> temINVALID_FLAG.
        flags = params.confidential_invalid_flags()

        def _set_flags(d: dict) -> None:
            d["Flags"] = flags

        mutate = _set_flags
    else:  # non_owner -> tefBAD_AUTH.
        if len(accounts) < 2:
            return
        impostor = choice([a for a in accounts.values() if a.address != src.address])
        wallet = impostor.wallet

    await submit_raw("ConfidentialMPTConvert", base, client, wallet, mutate)


# ── Send (confidential -> confidential) ──────────────────────────────


async def conf_mpt_send(
    accounts: dict[str, UserAccount],
    mpt_issuances: list[MPTokenIssuance],
    conf_issuances: list[ConfidentialMPTIssuance],
    client: AsyncJsonRpcClient,
) -> None:
    if params.should_send_faulty():
        return await _send_faulty(accounts, mpt_issuances, client)
    return await _send_valid(accounts, conf_issuances, client)


def _send_base(sender: str, destination: str, mpt_id: str) -> ConfidentialMPTSend:
    """Send base whose trivial on-curve blobs + exact-length proof pass preflight to preclaim."""
    return ConfidentialMPTSend(
        account=sender,
        destination=destination,
        mptoken_issuance_id=mpt_id,
        sender_encrypted_amount=params.confidential_ciphertext(),
        destination_encrypted_amount=params.confidential_ciphertext(),
        issuer_encrypted_amount=params.confidential_ciphertext(),
        amount_commitment=params.confidential_commitment(),
        balance_commitment=params.confidential_commitment(),
        zk_proof=params.confidential_send_proof(),
        fee=params.confidential_fee(),
    )


async def _send_valid(
    accounts: dict[str, UserAccount],
    conf_issuances: list[ConfidentialMPTIssuance],
    client: AsyncJsonRpcClient,
) -> None:
    if not cc.CRYPTO_AVAILABLE or not conf_issuances:
        return
    ci = choice(conf_issuances)
    if not ci.issuer_pubkey:
        return
    candidates = [h for h in ci.holders if h in accounts and accounts[h].elgamal_public_key]
    if len(candidates) < 2:
        return
    sender_addr = choice(candidates)
    dest_addr = choice([h for h in candidates if h != sender_addr])
    sender = accounts[sender_addr]
    sk, pk = sender.elgamal_private_key, sender.elgamal_public_key
    dest_pk = accounts[dest_addr].elgamal_public_key
    if not sk or not pk or not dest_pk:
        return
    holder = ci.holders[sender_addr]
    if holder.spending_balance <= 0:
        return
    amount = max(1, holder.spending_balance // randint(2, 10))
    # Builder binds account Sequence into the proof; stamp same seq on submit (else tecBAD_PROOF),
    # and stash the plaintext amount keyed by it so _on_conf_send can decrement spending_balance.
    try:
        seq = await cc.account_sequence(client.url, sender.address)
        base = await cc.build_send(
            client.url,
            sender.wallet,
            dest_addr,
            ci.mpt_issuance_id,
            amount,
            sk,
            pk,
            dest_pk,
            ci.issuer_pubkey,
        )
    except cc.BUILD_SKIP_ERRORS:
        return
    result = await submit_tx("ConfidentialMPTSend", base, client, sender.wallet, seq=seq)
    if result:
        _pending_send_amounts[(sender.address, seq, ci.mpt_issuance_id)] = (amount, dest_addr)


async def _send_faulty(
    accounts: dict[str, UserAccount],
    mpt_issuances: list[MPTokenIssuance],
    client: AsyncJsonRpcClient,
) -> None:
    if len(accounts) < 2:
        return
    acct_list = list(accounts.values())
    src = choice(acct_list)
    dst = choice([a for a in acct_list if a.address != src.address])
    real_id = choice(mpt_issuances).mpt_issuance_id if mpt_issuances else params.fake_mpt_id()

    mutation = choice(
        [
            "garbage_proof",
            "wrong_length_proof",
            "wrong_length_ciphertext",
            "empty_commitment",
            "self_send",
            "send_to_issuer",
            "fake_mpt_id",
            "invalid_flags",
            "non_owner",
            "fuzz",
        ]
    )

    if mutation == "fuzz":
        base = _send_base(src.address, dst.address, real_id)
        await submit_fuzzed("ConfidentialMPTSend", base, client, src.wallet)
        return

    base = _send_base(src.address, dst.address, real_id)
    wallet = src.wallet
    mutate: Callable[[dict], None] | None = None

    if mutation == "garbage_proof":
        # Bogus proof on a real issuance -> preclaim tecBAD_PROOF/tecOBJECT_NOT_FOUND/
        # tecNO_PERMISSION; the reliable validating-tec failure feeder.
        pass
    elif mutation == "wrong_length_proof":
        # -> temMALFORMED.
        proof = params.confidential_wrong_length_hex(params._SEND_PROOF_HEX_LEN)

        def _set_proof(d: dict) -> None:
            d["ZKProof"] = proof

        mutate = _set_proof
    elif mutation == "wrong_length_ciphertext":
        # -> temBAD_CIPHERTEXT.
        ct = params.confidential_wrong_length_hex(params._CIPHERTEXT_HEX_LEN)

        def _set_ct(d: dict) -> None:
            d["SenderEncryptedAmount"] = ct

        mutate = _set_ct
    elif mutation == "empty_commitment":
        # -> temMALFORMED.
        def _set_empty(d: dict) -> None:
            d["AmountCommitment"] = ""

        mutate = _set_empty
    elif mutation == "self_send":
        # account == destination -> temMALFORMED. Model rejects it at construction, so
        # build with a distinct dest and rewrite Destination in the dict.
        self_addr = src.address

        def _set_self(d: dict) -> None:
            d["Destination"] = self_addr

        mutate = _set_self
    elif mutation == "send_to_issuer":
        # destination == issuer -> temMALFORMED. Model rejects account==destination at
        # construction, so if src is the issuer, build distinct and rewrite Destination.
        if not mpt_issuances:
            return
        mpt = choice(mpt_issuances)
        issuer = accounts.get(mpt.issuer)
        if not issuer:
            return
        if issuer.address == src.address:
            issuer_addr = issuer.address

            def _set_issuer(d: dict) -> None:
                d["Destination"] = issuer_addr

            mutate = _set_issuer
        else:
            base = _send_base(src.address, issuer.address, mpt.mpt_issuance_id)
    elif mutation == "fake_mpt_id":
        # -> preclaim tecOBJECT_NOT_FOUND (validating-tec feeder).
        base = _send_base(src.address, dst.address, params.fake_mpt_id())
    elif mutation == "invalid_flags":
        # -> temINVALID_FLAG.
        flags = params.confidential_invalid_flags()

        def _set_flags(d: dict) -> None:
            d["Flags"] = flags

        mutate = _set_flags
    else:  # non_owner -> tefBAD_AUTH.
        impostor = choice([a for a in acct_list if a.address != src.address])
        wallet = impostor.wallet

    await submit_raw("ConfidentialMPTSend", base, client, wallet, mutate)


# ── ConvertBack (confidential -> public) ─────────────────────────────


async def conf_mpt_convert_back(
    accounts: dict[str, UserAccount],
    mpt_issuances: list[MPTokenIssuance],
    conf_issuances: list[ConfidentialMPTIssuance],
    client: AsyncJsonRpcClient,
) -> None:
    if params.should_send_faulty():
        return await _convert_back_faulty(accounts, mpt_issuances, client)
    return await _convert_back_valid(accounts, conf_issuances, client)


def _convert_back_base(account: str, mpt_id: str) -> ConfidentialMPTConvertBack:
    """ConvertBack base whose trivial blobs + exact-length proof pass preflight to preclaim."""
    return ConfidentialMPTConvertBack(
        account=account,
        mptoken_issuance_id=mpt_id,
        mpt_amount=params.confidential_mpt_amount(),
        holder_encrypted_amount=params.confidential_ciphertext(),
        issuer_encrypted_amount=params.confidential_ciphertext(),
        blinding_factor=params.confidential_blinding_factor(),
        balance_commitment=params.confidential_commitment(),
        zk_proof=params.confidential_convert_back_proof(),
        fee=params.confidential_fee(),
    )


async def _convert_back_valid(
    accounts: dict[str, UserAccount],
    conf_issuances: list[ConfidentialMPTIssuance],
    client: AsyncJsonRpcClient,
) -> None:
    if not cc.CRYPTO_AVAILABLE or not conf_issuances:
        return
    ci = choice(conf_issuances)
    if not ci.issuer_pubkey:
        return
    candidates = [h for h in ci.holders if h in accounts and accounts[h].elgamal_public_key]
    if not candidates:
        return
    holder_addr = choice(candidates)
    holder = accounts[holder_addr]
    sk, pk = holder.elgamal_private_key, holder.elgamal_public_key
    if not sk or not pk:
        return
    state = ci.holders[holder_addr]
    if state.spending_balance <= 0:
        return
    amount = max(1, state.spending_balance // randint(2, 10))
    # Builder binds account Sequence into the proof; stamp same seq on submit (else tecBAD_PROOF).
    try:
        seq = await cc.account_sequence(client.url, holder.address)
        base = await cc.build_convert_back(
            client.url,
            holder.wallet,
            ci.mpt_issuance_id,
            amount,
            sk,
            pk,
            ci.issuer_pubkey,
        )
    except cc.BUILD_SKIP_ERRORS:
        return
    await submit_tx("ConfidentialMPTConvertBack", base, client, holder.wallet, seq=seq)


async def _convert_back_faulty(
    accounts: dict[str, UserAccount],
    mpt_issuances: list[MPTokenIssuance],
    client: AsyncJsonRpcClient,
) -> None:
    if not accounts:
        return
    src = choice(list(accounts.values()))
    real_id = choice(mpt_issuances).mpt_issuance_id if mpt_issuances else params.fake_mpt_id()

    mutation = choice(
        [
            "garbage_proof",
            "wrong_length_proof",
            "wrong_length_ciphertext",
            "empty_commitment",
            "overdraw_amount",
            "fake_mpt_id",
            "invalid_flags",
            "non_owner",
            "fuzz",
        ]
    )

    if mutation == "fuzz":
        base = _convert_back_base(src.address, real_id)
        await submit_fuzzed("ConfidentialMPTConvertBack", base, client, src.wallet)
        return

    base = _convert_back_base(src.address, real_id)
    wallet = src.wallet
    mutate: Callable[[dict], None] | None = None

    if mutation == "garbage_proof":
        # Bogus proof on a real issuance -> preclaim tecBAD_PROOF/tecOBJECT_NOT_FOUND/
        # tecINSUFFICIENT_FUNDS; the reliable validating-tec failure feeder.
        pass
    elif mutation == "wrong_length_proof":
        # -> temMALFORMED.
        proof = params.confidential_wrong_length_hex(params._CONVERT_BACK_PROOF_HEX_LEN)

        def _set_proof(d: dict) -> None:
            d["ZKProof"] = proof

        mutate = _set_proof
    elif mutation == "wrong_length_ciphertext":
        # -> temBAD_CIPHERTEXT.
        ct = params.confidential_wrong_length_hex(params._CIPHERTEXT_HEX_LEN)

        def _set_ct(d: dict) -> None:
            d["HolderEncryptedAmount"] = ct

        mutate = _set_ct
    elif mutation == "empty_commitment":
        # -> temMALFORMED.
        def _set_empty(d: dict) -> None:
            d["BalanceCommitment"] = ""

        mutate = _set_empty
    elif mutation == "overdraw_amount":
        # Above plausible outstanding -> preclaim tecINSUFFICIENT_FUNDS; stays under 2^63-1.
        amt = params.confidential_mpt_amount() + randint(1_000_000, 9_999_999)

        def _set_overdraw(d: dict) -> None:
            d["MPTAmount"] = str(amt)

        mutate = _set_overdraw
    elif mutation == "fake_mpt_id":
        # -> preclaim tecOBJECT_NOT_FOUND (validating-tec feeder).
        base = _convert_back_base(src.address, params.fake_mpt_id())
    elif mutation == "invalid_flags":
        # -> temINVALID_FLAG.
        flags = params.confidential_invalid_flags()

        def _set_flags(d: dict) -> None:
            d["Flags"] = flags

        mutate = _set_flags
    else:  # non_owner -> tefBAD_AUTH.
        if len(accounts) < 2:
            return
        impostor = choice([a for a in accounts.values() if a.address != src.address])
        wallet = impostor.wallet

    await submit_raw("ConfidentialMPTConvertBack", base, client, wallet, mutate)


# ── Clawback (issuer reclaims confidential balance) ──────────────────


async def conf_mpt_clawback(
    accounts: dict[str, UserAccount],
    mpt_issuances: list[MPTokenIssuance],
    conf_issuances: list[ConfidentialMPTIssuance],
    client: AsyncJsonRpcClient,
) -> None:
    if params.should_send_faulty():
        return await _clawback_faulty(accounts, mpt_issuances, client)
    return await _clawback_valid(accounts, conf_issuances, client)


def _clawback_base(issuer: str, holder: str, mpt_id: str) -> ConfidentialMPTClawback:
    """Clawback base with a correct-length (64B) trivial proof construction and preflight accept."""
    return ConfidentialMPTClawback(
        account=issuer,
        holder=holder,
        mptoken_issuance_id=mpt_id,
        mpt_amount=params.confidential_mpt_amount(),
        zk_proof=params.confidential_clawback_proof(),
        fee=params.confidential_fee(),
    )


async def _clawback_valid(
    accounts: dict[str, UserAccount],
    conf_issuances: list[ConfidentialMPTIssuance],
    client: AsyncJsonRpcClient,
) -> None:
    if not cc.CRYPTO_AVAILABLE or not conf_issuances:
        return
    ci = choice(conf_issuances)
    if not ci.issuer_pubkey or not ci.issuer_privkey:
        return
    issuer = accounts.get(ci.issuer)
    if not issuer:
        return
    holders = [
        h
        for h, st in ci.holders.items()
        if h in accounts and accounts[h].elgamal_public_key and st.spending_balance > 0
    ]
    if not holders:
        return
    holder_addr = choice(holders)
    try:
        # Proof links the holder's on-ledger IssuerEncryptedBalance to the amount.
        enc_bal = await cc.issuer_encrypted_balance(client.url, holder_addr, ci.mpt_issuance_id)
        if not enc_bal:
            return
        # The equality proof must match the encrypted balance EXACTLY; tracked
        # spending_balance drifts under concurrent sends/converts (-> tecBAD_PROOF),
        # so decrypt the ledger truth and claw that.
        amount = await cc.decrypt(ci.issuer_privkey, enc_bal)
        if amount <= 0:
            return
        # Builder binds issuer Sequence into the proof; stamp same seq on submit
        # (else tecBAD_PROOF).
        seq = await cc.account_sequence(client.url, issuer.address)
        base = await cc.build_clawback(
            client.url,
            issuer.wallet,
            holder_addr,
            ci.mpt_issuance_id,
            amount,
            ci.issuer_privkey,
            ci.issuer_pubkey,
            enc_bal,
        )
    except cc.BUILD_SKIP_ERRORS:
        return
    await submit_tx("ConfidentialMPTClawback", base, client, issuer.wallet, seq=seq)


async def _clawback_faulty(
    accounts: dict[str, UserAccount],
    mpt_issuances: list[MPTokenIssuance],
    client: AsyncJsonRpcClient,
) -> None:
    if len(accounts) < 2:
        return
    acct_list = list(accounts.values())
    src = choice(acct_list)
    holder = choice([a for a in acct_list if a.address != src.address])
    real_id = choice(mpt_issuances).mpt_issuance_id if mpt_issuances else params.fake_mpt_id()

    # Reaching preclaim needs a real issuance signed by its issuer (preflight: account==issuer).
    # None when unavailable (then only seen-bucket vectors are possible).
    real_issuer_mpt = None
    for mpt in mpt_issuances:
        if mpt.issuer in accounts and any(a.address != mpt.issuer for a in acct_list):
            real_issuer_mpt = mpt
            break

    mutation = choice(
        [
            "garbage_proof",
            "wrong_length_proof",
            "non_issuer",
            "self_clawback",
            "fake_mpt_id",
            "invalid_flags",
            "non_owner",
            "fuzz",
        ]
    )

    if mutation == "fuzz":
        base = _clawback_base(src.address, holder.address, real_id)
        await submit_fuzzed("ConfidentialMPTClawback", base, client, src.wallet)
        return

    base = _clawback_base(src.address, holder.address, real_id)
    wallet = src.wallet
    mutate: Callable[[dict], None] | None = None

    if mutation == "garbage_proof":
        # Bogus proof signed by the REAL issuer -> preclaim tecBAD_PROOF/tecOBJECT_NOT_FOUND/
        # tecINSUFFICIENT_FUNDS; the reliable validating-tec failure feeder. No real issuer ->
        # falls through to a self-signed seen-only temMALFORMED.
        if real_issuer_mpt is not None:
            issuer = accounts[real_issuer_mpt.issuer]
            target = choice([a for a in acct_list if a.address != issuer.address])
            base = _clawback_base(issuer.address, target.address, real_issuer_mpt.mpt_issuance_id)
            wallet = issuer.wallet
    elif mutation == "wrong_length_proof":
        # -> temMALFORMED.
        bad = params.confidential_wrong_length_hex(params._CLAWBACK_PROOF_HEX_LEN)

        def _set_proof(d: dict) -> None:
            d["ZKProof"] = bad

        mutate = _set_proof
    elif mutation == "non_issuer":
        # Non-issuer clawback -> temMALFORMED. Target holder must differ from impostor
        # (model rejects account==holder at construction).
        if mpt_issuances:
            mpt = choice(mpt_issuances)
            non_issuers = [a for a in acct_list if a.address != mpt.issuer]
            if non_issuers:
                impostor = choice(non_issuers)
                targets = [a for a in acct_list if a.address != impostor.address]
                if targets:
                    target = choice(targets)
                    base = _clawback_base(impostor.address, target.address, mpt.mpt_issuance_id)
                    wallet = impostor.wallet
    elif mutation == "self_clawback":
        # account == holder -> temMALFORMED. Model rejects it at construction, so build
        # with a distinct holder and rewrite Holder to src in the dict.
        self_addr = src.address

        def _set_self(d: dict) -> None:
            d["Holder"] = self_addr

        mutate = _set_self
    elif mutation == "fake_mpt_id":
        # Issuer derived from a random id won't equal src -> temMALFORMED before preclaim read.
        base = _clawback_base(src.address, holder.address, params.fake_mpt_id())
    elif mutation == "invalid_flags":
        # -> temINVALID_FLAG.
        flags = params.confidential_invalid_flags()

        def _set_flags(d: dict) -> None:
            d["Flags"] = flags

        mutate = _set_flags
    else:  # non_owner -> tefBAD_AUTH.
        impostor = choice([a for a in acct_list if a.address != src.address])
        wallet = impostor.wallet

    await submit_raw("ConfidentialMPTClawback", base, client, wallet, mutate)


__all__ = [
    "conf_mpt_clawback",
    "conf_mpt_convert",
    "conf_mpt_convert_back",
    "conf_mpt_merge_inbox",
    "conf_mpt_send",
]

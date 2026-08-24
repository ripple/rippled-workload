"""Unit tests for the Dynamic MPT (XLS-0094) issuance-set handler.

Fully offline: the submit layer (submit_tx / submit_raw / submit_fuzzed) and
randoms.choice are monkeypatched, so no rippled node is required. Randomness is
made deterministic via a Chooser that returns configured mutations/flags/indices.
"""

import asyncio

from xrpl.models.transactions import (
    MPTokenIssuanceCreate,
    MPTokenIssuanceCreateFlag,
)
from xrpl.models.transactions import MPTokenIssuanceImmutableFlag as IM
from xrpl.wallet import Wallet

from workload import params, setup
from workload.models import MPTokenIssuance, UserAccount
from workload.transactions import mpt_dynamic as mpt

# Immutable cohort bitmask (XLS-0094 opt-out): freeze every capability +
# metadata/transfer-fee so no later mutation can stick. Mirrors
# setup._DYNAMIC_IMMUTABLE_FLAGS.
_IMMUTABLE_FLAGS = int(
    IM.TIF_MPT_CAN_LOCK
    | IM.TIF_MPT_REQUIRE_AUTH
    | IM.TIF_MPT_CAN_ESCROW
    | IM.TIF_MPT_CAN_TRADE
    | IM.TIF_MPT_CAN_TRANSFER
    | IM.TIF_MPT_CAN_CLAWBACK
    | IM.TIF_MPT_CAN_HOLD_CONFIDENTIAL_BALANCE
    | IM.TIF_MPT_METADATA
    | IM.TIF_MPT_TRANSFER_FEE
)
MUT_ID = "A0" * 24  # dynamic + fully mutable issuance (immutable_flags == 0)
IMM_ID = "B1" * 24  # dynamic + frozen issuance (immutable_flags != 0)

_MUTATIONS = {
    "flag_enable", "metadata", "transfer_fee",
    "fuzz", "fake_issuance", "non_issuer", "immutable_mutation", "oversize_metadata",
}


class Chooser:
    """Deterministic replacement for randoms.choice."""

    def __init__(self, mutation=None, index=0, flag=0x08):
        self.mutation, self.index, self.flag = mutation, index, flag

    def __call__(self, seq):
        seq = list(seq)
        first = seq[0]
        if isinstance(first, bool):
            return seq[self.index]
        if isinstance(first, int):
            return self.flag
        if isinstance(first, str) and first in _MUTATIONS:
            return self.mutation
        return seq[self.index]


class Recorder:
    def __init__(self):
        self.txs, self.raws, self.fuzzes = [], [], []

    async def tx(self, name, txn, client, wallet):
        self.txs.append((name, txn, wallet))

    async def raw(self, name, base, client, wallet, mutate=None):
        d = base.to_xrpl()
        if mutate is not None:
            mutate(d)
        self.raws.append((name, d))

    async def fuzz(self, name, base, client, wallet):
        self.fuzzes.append((name, base))


def _run(coro):
    return asyncio.run(coro)


def _accounts():
    a, b = UserAccount(wallet=Wallet.create()), UserAccount(wallet=Wallet.create())
    return {a.address: a, b.address: b}


def _issuances(accounts):
    addrs = list(accounts)
    return [
        MPTokenIssuance(
            issuer=addrs[0],
            mpt_issuance_id=MUT_ID,
            dynamic=True,
            immutable_flags=0,
            can_transfer=True,
        ),
        MPTokenIssuance(
            issuer=addrs[0],
            mpt_issuance_id=IMM_ID,
            dynamic=True,
            immutable_flags=_IMMUTABLE_FLAGS,
        ),
    ]


def _install(monkeypatch, rec, *, mutation=None, index=0, flag=0x08, faulty=False):
    monkeypatch.setattr(mpt, "choice", Chooser(mutation, index, flag))
    monkeypatch.setattr(mpt, "submit_tx", rec.tx)
    monkeypatch.setattr(mpt, "submit_raw", rec.raw)
    monkeypatch.setattr(mpt, "submit_fuzzed", rec.fuzz)
    monkeypatch.setattr(params, "should_send_faulty", lambda: faulty)


# ── Filters ──────────────────────────────────────────────────────────
def test_mutable_issuances_excludes_immutable_unowned_and_nondynamic():
    accts = _accounts()
    isss = _issuances(accts)
    addrs = list(accts)
    foreign = MPTokenIssuance(
        issuer="rForeignIssuerNotInAccounts9999",
        mpt_issuance_id="C2" * 24,
        dynamic=True,
    )
    # Regular MPT cohort (dynamic=False): fully mutable under the new model but
    # must be excluded so the handler never disturbs the DEX/AMM + XLS-82
    # cohorts [0..5].
    regular = MPTokenIssuance(issuer=addrs[0], mpt_issuance_id="D3" * 24, dynamic=False)
    dyn = mpt._mutable_issuances(accts, [*isss, foreign, regular])
    assert [m.mpt_issuance_id for m in dyn] == [MUT_ID]


def test_immutable_issuances_filter():
    accts = _accounts()
    imm = mpt._immutable_issuances(accts, _issuances(accts))
    assert [m.mpt_issuance_id for m in imm] == [IMM_ID]


# ── Base builder ─────────────────────────────────────────────────────
def test_base_builds_set_enable_from_issuer(monkeypatch):
    accts = _accounts()
    monkeypatch.setattr(mpt, "choice", Chooser(flag=0x08))
    built = mpt._mpt_issuance_set_dynamic_base(accts, _issuances(accts))
    assert built is not None
    txn, wallet = built
    d = txn.to_xrpl()
    assert d["MPTokenIssuanceID"] == MUT_ID
    assert d["Flags"] in mpt._SET_ENABLE_FLAGS
    assert d["Account"] == wallet.address


# ── Valid paths ──────────────────────────────────────────────────────
def test_valid_flag_enable_submits_typed(monkeypatch):
    accts, rec = _accounts(), Recorder()
    _install(monkeypatch, rec, mutation="flag_enable")
    _run(mpt.mpt_issuance_set_dynamic(accts, _issuances(accts), None))
    assert len(rec.txs) == 1 and rec.txs[0][0] == "DynamicMPTSet"
    assert not rec.raws and not rec.fuzzes
    d = rec.txs[0][1].to_xrpl()
    assert d["Flags"] in mpt._SET_ENABLE_FLAGS


def test_valid_metadata_submits_typed(monkeypatch):
    accts, rec = _accounts(), Recorder()
    _install(monkeypatch, rec, mutation="metadata")
    _run(mpt.mpt_issuance_set_dynamic(accts, _issuances(accts), None))
    assert len(rec.txs) == 1 and not rec.raws
    d = rec.txs[0][1].to_xrpl()
    assert d["MPTokenMetadata"] and "Flags" not in d


def test_valid_transfer_fee_submits_typed(monkeypatch):
    accts, rec = _accounts(), Recorder()
    _install(monkeypatch, rec, mutation="transfer_fee")
    _run(mpt.mpt_issuance_set_dynamic(accts, _issuances(accts), None))
    assert len(rec.txs) == 1 and not rec.raws
    d = rec.txs[0][1].to_xrpl()
    assert int(d["TransferFee"]) > 0 and "Flags" not in d


# ── Faulty paths ─────────────────────────────────────────────────────
def test_faulty_fuzz_rides_submit_fuzzed(monkeypatch):
    accts, rec = _accounts(), Recorder()
    _install(monkeypatch, rec, mutation="fuzz", faulty=True)
    _run(mpt.mpt_issuance_set_dynamic(accts, _issuances(accts), None))
    assert len(rec.fuzzes) == 1 and rec.fuzzes[0][0] == "DynamicMPTSet"


def test_faulty_fake_issuance_uses_fake_id(monkeypatch):
    accts, rec = _accounts(), Recorder()
    _install(monkeypatch, rec, mutation="fake_issuance", faulty=True)
    _run(mpt.mpt_issuance_set_dynamic(accts, _issuances(accts), None))
    assert len(rec.txs) == 1
    d = rec.txs[0][1].to_xrpl()
    assert d["MPTokenIssuanceID"] not in (MUT_ID, IMM_ID)


def test_faulty_non_issuer_submits_as_other(monkeypatch):
    accts, rec = _accounts(), Recorder()
    _install(monkeypatch, rec, mutation="non_issuer", faulty=True)
    _run(mpt.mpt_issuance_set_dynamic(accts, _issuances(accts), None))
    assert len(rec.txs) == 1
    name, txn, wallet = rec.txs[0]
    d = txn.to_xrpl()
    isss = _issuances(accts)
    assert d["MPTokenIssuanceID"] == MUT_ID and d["Account"] != isss[0].issuer


def test_faulty_immutable_mutation_targets_immutable(monkeypatch):
    accts, rec = _accounts(), Recorder()
    _install(monkeypatch, rec, mutation="immutable_mutation", faulty=True)
    _run(mpt.mpt_issuance_set_dynamic(accts, _issuances(accts), None))
    assert len(rec.txs) == 1 and not rec.raws
    d = rec.txs[0][1].to_xrpl()
    assert d["MPTokenIssuanceID"] == IMM_ID and d["MPTokenMetadata"]


def test_faulty_oversize_metadata_exceeds_limit(monkeypatch):
    accts, rec = _accounts(), Recorder()
    _install(monkeypatch, rec, mutation="oversize_metadata", faulty=True)
    _run(mpt.mpt_issuance_set_dynamic(accts, _issuances(accts), None))
    assert len(rec.raws) == 1
    _, d = rec.raws[0]
    assert "Flags" not in d and len(d["MPTokenMetadata"]) > 2048


# ── Dispatcher routing ───────────────────────────────────────────────
def test_dispatch_routes_valid_vs_faulty(monkeypatch):
    accts, rec = _accounts(), Recorder()
    _install(monkeypatch, rec, mutation="flag_enable", faulty=False)
    _run(mpt.mpt_issuance_set_dynamic(accts, _issuances(accts), None))
    assert rec.txs and not rec.fuzzes
    rec.txs.clear()
    _install(monkeypatch, rec, mutation="fuzz", faulty=True)
    _run(mpt.mpt_issuance_set_dynamic(accts, _issuances(accts), None))
    assert rec.fuzzes and not rec.txs


# ── Empty-state preconditions ────────────────────────────────────────
def test_no_dynamic_issuances_is_noop(monkeypatch):
    rec = Recorder()
    _install(monkeypatch, rec, mutation="flag_enable")
    _run(mpt.mpt_issuance_set_dynamic({}, [], None))
    assert not rec.txs and not rec.raws and not rec.fuzzes


# ── Setup cohort construction (XLS-0094 opt-out) ─────────────────────
def test_setup_cohorts_construct_expected_immutable_flags():
    """Mutable cohort (53-55) sends NO ImmutableFlags; immutable cohort (56-58)
    freezes every capability + metadata + transfer-fee bit."""
    addr = Wallet.create().address
    lock_xfer = (
        MPTokenIssuanceCreateFlag.TF_MPT_CAN_LOCK | MPTokenIssuanceCreateFlag.TF_MPT_CAN_TRANSFER
    )
    mutable = MPTokenIssuanceCreate(account=addr, flags=lock_xfer).to_xrpl()
    immutable = MPTokenIssuanceCreate(
        account=addr, flags=lock_xfer, immutable_flags=setup._DYNAMIC_IMMUTABLE_FLAGS
    ).to_xrpl()

    # Mutable cohort stays fully mutable: no create-time freeze.
    assert "ImmutableFlags" not in mutable

    # Immutable cohort freezes the full bitmask, and every TIF_* bit is set.
    assert int(immutable["ImmutableFlags"]) == int(setup._DYNAMIC_IMMUTABLE_FLAGS)
    assert int(setup._DYNAMIC_IMMUTABLE_FLAGS) == _IMMUTABLE_FLAGS
    for bit in IM:
        assert int(setup._DYNAMIC_IMMUTABLE_FLAGS) & int(bit), f"{bit.name} not frozen"

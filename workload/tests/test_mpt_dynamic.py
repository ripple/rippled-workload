import asyncio
from types import SimpleNamespace

from xrpl.models.transactions import MPTokenIssuanceImmutableFlag as IM
from xrpl.models.transactions import MPTokenIssuanceSetFlag as SF
from xrpl.wallet import Wallet

from workload import params
from workload.models import MPTokenIssuance, UserAccount
from workload.transactions import _on_mpt_issuance_set
from workload.transactions import mpt_dynamic as dynamic
from workload.ws_listener import _is_dynamic_mpt_set

MUTABLE_ID = "A0" * 24
FROZEN_ID = "B1" * 24


def _accounts():
    first, second = UserAccount(Wallet.create()), UserAccount(Wallet.create())
    return {first.address: first, second.address: second}


def _issuances(accounts):
    issuer = next(iter(accounts))
    return [
        MPTokenIssuance(issuer, MUTABLE_ID, dynamic=True),
        MPTokenIssuance(
            issuer,
            FROZEN_ID,
            immutable_flags=dynamic._ALL_IMMUTABLE_FLAGS,
            dynamic=True,
        ),
        MPTokenIssuance(issuer, "C2" * 24),
    ]


def _run(coro):
    return asyncio.run(coro)


def test_dynamic_filter_scopes_cohorts():
    accounts = _accounts()
    issuances = _issuances(accounts)
    assert dynamic._dynamic_issuances(accounts, issuances, immutable=False) == [issuances[0]]
    assert dynamic._dynamic_issuances(accounts, issuances, immutable=True) == [issuances[1]]


def test_valid_flag_enable_excludes_require_auth(monkeypatch):
    accounts = _accounts()
    submitted = []

    def choose(seq):
        if seq and isinstance(seq[0], str):
            return "flag_enable"
        return list(seq)[0]

    async def submit(name, txn, client, wallet):
        submitted.append((name, txn.to_xrpl()))

    monkeypatch.setattr(dynamic, "choice", choose)
    monkeypatch.setattr(dynamic, "submit_tx", submit)
    _run(dynamic._dynamic_valid(accounts, _issuances(accounts), None))
    assert submitted[0][0] == "DynamicMPTSet"
    assert submitted[0][1]["Flags"] != int(SF.TF_MPT_SET_REQUIRE_AUTH)


def test_valid_can_add_immutable_flag(monkeypatch):
    accounts = _accounts()
    submitted = []

    def choose(seq):
        values = list(seq)
        return "immutable" if values and isinstance(values[0], str) else values[0]

    async def submit(name, txn, client, wallet):
        submitted.append(txn.to_xrpl())

    monkeypatch.setattr(dynamic, "choice", choose)
    monkeypatch.setattr(dynamic, "submit_tx", submit)
    _run(dynamic._dynamic_valid(accounts, _issuances(accounts), None))
    assert submitted[0]["ImmutableFlags"] == int(IM.TIF_MPT_CAN_LOCK)


def test_faulty_fuzz_uses_fuzz_submitter(monkeypatch):
    accounts = _accounts()
    seen = []
    monkeypatch.setattr(dynamic, "choice", lambda seq: "fuzz" if "fuzz" in seq else list(seq)[0])

    async def fuzz(name, base, client, wallet):
        seen.append(name)

    monkeypatch.setattr(dynamic, "submit_fuzzed", fuzz)
    _run(dynamic._dynamic_faulty(accounts, _issuances(accounts), None))
    assert seen == ["DynamicMPTSet"]


def test_state_updater_tracks_capability_and_immutability():
    mpt = MPTokenIssuance("rIssuer", MUTABLE_ID)
    workload = SimpleNamespace(mpt_issuances=[mpt])
    _on_mpt_issuance_set(
        workload,
        {
            "MPTokenIssuanceID": MUTABLE_ID,
            "Flags": int(SF.TF_MPT_SET_CAN_TRADE),
            "ImmutableFlags": int(IM.TIF_MPT_CAN_TRANSFER),
        },
        {},
    )
    assert mpt.can_trade
    assert mpt.immutable_flags == int(IM.TIF_MPT_CAN_TRANSFER)


def test_listener_routes_only_dynamic_fields_and_cohort():
    dynamic_mpt = MPTokenIssuance("rIssuer", MUTABLE_ID, dynamic=True)
    regular_mpt = MPTokenIssuance("rIssuer", FROZEN_ID)
    workload = SimpleNamespace(mpt_issuances=[dynamic_mpt, regular_mpt])
    base = {"TransactionType": "MPTokenIssuanceSet", "MPTokenMetadata": "AA"}
    assert _is_dynamic_mpt_set(workload, {**base, "MPTokenIssuanceID": MUTABLE_ID})
    assert not _is_dynamic_mpt_set(workload, {**base, "MPTokenIssuanceID": FROZEN_ID})
    assert not _is_dynamic_mpt_set(
        workload,
        {"TransactionType": "MPTokenIssuanceSet", "MPTokenIssuanceID": MUTABLE_ID, "Flags": 1},
    )


def test_transfer_fee_generator_is_in_range():
    assert 0 <= params.mpt_transfer_fee() <= 50_000

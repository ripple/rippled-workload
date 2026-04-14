"""Lending Protocol transaction generators for the antithesis workload.

Lending requires a vault to exist first, then a broker is created on that vault,
then loans are taken against the broker.
"""

from xrpl.asyncio.clients import AsyncJsonRpcClient
from xrpl.asyncio.transaction import autofill_and_sign
from xrpl.asyncio.transaction import submit as xrpl_submit
from xrpl.models.transactions import (
    LoanBrokerCoverDeposit,
    LoanBrokerCoverWithdraw,
    LoanBrokerDelete,
    LoanBrokerSet,
    LoanDelete,
    LoanManage,
    LoanPay,
    LoanSet,
)
from xrpl.models.transactions.loan_manage import LoanManageFlag
from xrpl.transaction.counterparty_signer import sign_loan_set_by_counterparty

from workload import params
from workload.assertions import tx_submitted
from workload.models import Loan, LoanBroker, UserAccount, Vault
from workload.randoms import choice, randint
from workload.submit import submit_tx

# ── Loan Broker Set ──────────────────────────────────────────────────


async def loan_broker_set(
    accounts: dict[str, UserAccount],
    vaults: list[Vault],
    loan_brokers: list[LoanBroker],
    client: AsyncJsonRpcClient,
) -> None:
    if params.should_send_faulty():
        return await _loan_broker_set_faulty(accounts, vaults, loan_brokers, client)
    return await _loan_broker_set_valid(accounts, vaults, loan_brokers, client)


async def _loan_broker_set_valid(
    accounts: dict[str, UserAccount],
    vaults: list[Vault],
    loan_brokers: list[LoanBroker],
    client: AsyncJsonRpcClient,
) -> None:
    if not vaults:
        return
    vault = choice(vaults)
    if vault.owner not in accounts:
        return
    owner = accounts[vault.owner]
    txn = LoanBrokerSet(
        account=owner.address,
        vault_id=vault.vault_id,
        management_fee_rate=params.loan_broker_management_fee_rate(),
        # Spec: "Either both are zero, or both are non-zero"
        cover_rate_minimum=(crm := params.loan_broker_cover_rate_minimum()),
        cover_rate_liquidation=params.loan_broker_cover_rate_liquidation() if crm > 0 else 0,
        debt_maximum=params.loan_broker_debt_maximum(),
        data=params.loan_broker_data(),
    )
    await submit_tx("LoanBrokerSet", txn, client, owner.wallet)


async def _loan_broker_set_faulty(
    accounts: dict[str, UserAccount],
    vaults: list[Vault],
    loan_brokers: list[LoanBroker],
    client: AsyncJsonRpcClient,
) -> None:
    if not accounts:
        return
    acc = choice(list(accounts.values()))
    mutation = choice(["nonexistent_vault", "cover_rate_asymmetry", "non_vault_owner"])

    if mutation == "nonexistent_vault":
        txn = LoanBrokerSet(
            account=acc.address,
            vault_id=params.fake_id(),
            management_fee_rate=params.loan_broker_management_fee_rate(),
            cover_rate_minimum=0,
            cover_rate_liquidation=0,
            debt_maximum=params.loan_broker_debt_maximum(),
            data=params.loan_broker_data(),
        )
        await submit_tx("LoanBrokerSet", txn, client, acc.wallet)

    elif mutation == "cover_rate_asymmetry":
        if not vaults:
            return
        vault = choice(vaults)
        if vault.owner not in accounts:
            return
        owner = accounts[vault.owner]
        txn = LoanBrokerSet(
            account=owner.address,
            vault_id=vault.vault_id,
            management_fee_rate=params.loan_broker_management_fee_rate(),
            cover_rate_minimum=1000,
            cover_rate_liquidation=0,
            debt_maximum=params.loan_broker_debt_maximum(),
            data=params.loan_broker_data(),
        )
        await submit_tx("LoanBrokerSet", txn, client, owner.wallet)

    elif mutation == "non_vault_owner":
        if not vaults:
            return
        vault = choice(vaults)
        non_owners = [a for a in accounts.values() if a.address != vault.owner]
        if not non_owners:
            return
        impostor = choice(non_owners)
        txn = LoanBrokerSet(
            account=impostor.address,
            vault_id=vault.vault_id,
            management_fee_rate=params.loan_broker_management_fee_rate(),
            cover_rate_minimum=0,
            cover_rate_liquidation=0,
            debt_maximum=params.loan_broker_debt_maximum(),
            data=params.loan_broker_data(),
        )
        await submit_tx("LoanBrokerSet", txn, client, impostor.wallet)


# ── Loan Broker Delete ───────────────────────────────────────────────


async def loan_broker_delete(
    accounts: dict[str, UserAccount], loan_brokers: list[LoanBroker], client: AsyncJsonRpcClient
) -> None:
    if params.should_send_faulty():
        return await _loan_broker_delete_faulty(accounts, loan_brokers, client)
    return await _loan_broker_delete_valid(accounts, loan_brokers, client)


async def _loan_broker_delete_valid(
    accounts: dict[str, UserAccount], loan_brokers: list[LoanBroker], client: AsyncJsonRpcClient
) -> None:
    if not loan_brokers:
        return
    broker = choice(loan_brokers)
    if broker.owner not in accounts:
        return
    owner = accounts[broker.owner]
    txn = LoanBrokerDelete(
        account=owner.address,
        loan_broker_id=broker.loan_broker_id,
    )
    await submit_tx("LoanBrokerDelete", txn, client, owner.wallet)


async def _loan_broker_delete_faulty(
    accounts: dict[str, UserAccount], loan_brokers: list[LoanBroker], client: AsyncJsonRpcClient
) -> None:
    if not accounts:
        return
    mutation = choice(["nonexistent_broker", "non_owner"])

    if mutation == "nonexistent_broker":
        acc = choice(list(accounts.values()))
        txn = LoanBrokerDelete(
            account=acc.address,
            loan_broker_id=params.fake_id(),
        )
        await submit_tx("LoanBrokerDelete", txn, client, acc.wallet)

    elif mutation == "non_owner":
        if not loan_brokers:
            return
        broker = choice(loan_brokers)
        non_owners = [a for a in accounts.values() if a.address != broker.owner]
        if not non_owners:
            return
        impostor = choice(non_owners)
        txn = LoanBrokerDelete(
            account=impostor.address,
            loan_broker_id=broker.loan_broker_id,
        )
        await submit_tx("LoanBrokerDelete", txn, client, impostor.wallet)


# ── Loan Broker Cover Deposit ────────────────────────────────────────


async def loan_broker_cover_deposit(
    accounts: dict[str, UserAccount], loan_brokers: list[LoanBroker], client: AsyncJsonRpcClient
) -> None:
    if params.should_send_faulty():
        return await _loan_broker_cover_deposit_faulty(accounts, loan_brokers, client)
    return await _loan_broker_cover_deposit_valid(accounts, loan_brokers, client)


async def _loan_broker_cover_deposit_valid(
    accounts: dict[str, UserAccount], loan_brokers: list[LoanBroker], client: AsyncJsonRpcClient
) -> None:
    if not loan_brokers:
        return
    broker = choice(loan_brokers)
    if broker.owner not in accounts:
        return
    owner = accounts[broker.owner]
    txn = LoanBrokerCoverDeposit(
        account=owner.address,
        loan_broker_id=broker.loan_broker_id,
        amount=params.loan_cover_deposit_amount(),
    )
    await submit_tx("LoanBrokerCoverDeposit", txn, client, owner.wallet)


async def _loan_broker_cover_deposit_faulty(
    accounts: dict[str, UserAccount], loan_brokers: list[LoanBroker], client: AsyncJsonRpcClient
) -> None:
    if not accounts:
        return
    acc = choice(list(accounts.values()))
    mutation = choice(["nonexistent_broker", "zero_deposit"])

    if mutation == "nonexistent_broker":
        txn = LoanBrokerCoverDeposit(
            account=acc.address,
            loan_broker_id=params.fake_id(),
            amount=params.loan_cover_deposit_amount(),
        )
        await submit_tx("LoanBrokerCoverDeposit", txn, client, acc.wallet)

    elif mutation == "zero_deposit":
        if not loan_brokers:
            return
        broker = choice(loan_brokers)
        if broker.owner not in accounts:
            return
        owner = accounts[broker.owner]
        txn = LoanBrokerCoverDeposit(
            account=owner.address,
            loan_broker_id=broker.loan_broker_id,
            amount="0",
        )
        await submit_tx("LoanBrokerCoverDeposit", txn, client, owner.wallet)


# ── Loan Broker Cover Withdraw ───────────────────────────────────────


async def loan_broker_cover_withdraw(
    accounts: dict[str, UserAccount], loan_brokers: list[LoanBroker], client: AsyncJsonRpcClient
) -> None:
    if params.should_send_faulty():
        return await _loan_broker_cover_withdraw_faulty(accounts, loan_brokers, client)
    return await _loan_broker_cover_withdraw_valid(accounts, loan_brokers, client)


def _state_aware_cover_withdraw_amount(broker: LoanBroker) -> str:
    """Generate a cover withdraw amount informed by tracked cover balance."""
    if broker.cover_balance <= 0:
        return params.loan_cover_deposit_amount()
    strategy = choice(["exact", "half", "small"])
    if strategy == "exact":
        return str(broker.cover_balance)
    elif strategy == "half":
        return str(max(1, broker.cover_balance // 2))
    return str(max(1, broker.cover_balance // 4))


async def _loan_broker_cover_withdraw_valid(
    accounts: dict[str, UserAccount], loan_brokers: list[LoanBroker], client: AsyncJsonRpcClient
) -> None:
    if not loan_brokers:
        return
    broker = choice(loan_brokers)
    if broker.owner not in accounts:
        return
    owner = accounts[broker.owner]
    txn = LoanBrokerCoverWithdraw(
        account=owner.address,
        loan_broker_id=broker.loan_broker_id,
        amount=_state_aware_cover_withdraw_amount(broker),
    )
    await submit_tx("LoanBrokerCoverWithdraw", txn, client, owner.wallet)


async def _loan_broker_cover_withdraw_faulty(
    accounts: dict[str, UserAccount], loan_brokers: list[LoanBroker], client: AsyncJsonRpcClient
) -> None:
    if not accounts:
        return
    mutation = choice(["nonexistent_broker", "excessive_withdrawal", "overdraw"])

    if mutation == "overdraw":
        if not loan_brokers:
            return
        broker = choice(loan_brokers)
        if broker.owner not in accounts:
            return
        owner = accounts[broker.owner]
        if broker.cover_balance > 0:
            amount = str(broker.cover_balance + randint(1, 1_000_000))
        else:
            amount = params.loan_cover_deposit_amount()
        txn = LoanBrokerCoverWithdraw(
            account=owner.address,
            loan_broker_id=broker.loan_broker_id,
            amount=amount,
        )
        await submit_tx("LoanBrokerCoverWithdraw", txn, client, owner.wallet)
        return

    if mutation == "nonexistent_broker":
        acc = choice(list(accounts.values()))
        txn = LoanBrokerCoverWithdraw(
            account=acc.address,
            loan_broker_id=params.fake_id(),
            amount=params.loan_cover_deposit_amount(),
        )
        await submit_tx("LoanBrokerCoverWithdraw", txn, client, acc.wallet)

    elif mutation == "excessive_withdrawal":
        if not loan_brokers:
            return
        broker = choice(loan_brokers)
        if broker.owner not in accounts:
            return
        owner = accounts[broker.owner]
        txn = LoanBrokerCoverWithdraw(
            account=owner.address,
            loan_broker_id=broker.loan_broker_id,
            amount=str(10**18),
        )
        await submit_tx("LoanBrokerCoverWithdraw", txn, client, owner.wallet)


# ── Loan Set ─────────────────────────────────────────────────────────


async def loan_set(
    accounts: dict[str, UserAccount],
    loan_brokers: list[LoanBroker],
    loans: list[Loan],
    client: AsyncJsonRpcClient,
) -> None:
    if params.should_send_faulty():
        return await _loan_set_faulty(accounts, loan_brokers, loans, client)
    return await _loan_set_valid(accounts, loan_brokers, loans, client)


async def _loan_set_valid(
    accounts: dict[str, UserAccount],
    loan_brokers: list[LoanBroker],
    loans: list[Loan],
    client: AsyncJsonRpcClient,
) -> None:
    if not loan_brokers:
        return
    broker = choice(loan_brokers)
    if broker.owner not in accounts:
        return
    broker_wallet = accounts[broker.owner].wallet
    other_accounts = [a for a in accounts if a != broker.owner]
    if not other_accounts:
        return
    borrower_id = choice(other_accounts)
    borrower = accounts[borrower_id]
    pi = params.loan_payment_interval()
    txn = LoanSet(
        account=borrower.address,
        loan_broker_id=broker.loan_broker_id,
        counterparty=broker.owner,
        principal_requested=params.loan_principal(),
        interest_rate=params.loan_interest_rate(),
        payment_total=params.loan_payment_total(),
        payment_interval=pi,
        grace_period=params.loan_grace_period(pi),
    )
    # LoanSet requires counterparty co-signing: borrower signs, then broker co-signs
    signed = await autofill_and_sign(txn, client, borrower.wallet)
    cosigned = sign_loan_set_by_counterparty(broker_wallet, signed)
    tx_submitted("LoanSet", txn)
    await xrpl_submit(cosigned.tx, client)
    # TODO: re-enable as structured log for tx sequence analysis
    # {"tx_type": "LoanSet", "engine_result": response.result.get("engine_result", "")}


async def _loan_set_faulty(
    accounts: dict[str, UserAccount],
    loan_brokers: list[LoanBroker],
    loans: list[Loan],
    client: AsyncJsonRpcClient,
) -> None:
    if not accounts:
        return
    mutation = choice(["nonexistent_broker", "no_cosign"])

    if mutation == "nonexistent_broker":
        acc = choice(list(accounts.values()))
        other_accounts = [a for a in accounts.values() if a.address != acc.address]
        if not other_accounts:
            return
        counterparty = choice(other_accounts)
        pi = params.loan_payment_interval()
        txn = LoanSet(
            account=acc.address,
            loan_broker_id=params.fake_id(),
            counterparty=counterparty.address,
            principal_requested=params.loan_principal(),
            interest_rate=params.loan_interest_rate(),
            payment_total=params.loan_payment_total(),
            payment_interval=pi,
            grace_period=params.loan_grace_period(pi),
        )
        await submit_tx("LoanSet", txn, client, acc.wallet)

    elif mutation == "no_cosign":
        if not loan_brokers:
            return
        broker = choice(loan_brokers)
        if broker.owner not in accounts:
            return
        other_accounts = [a for a in accounts.values() if a.address != broker.owner]
        if not other_accounts:
            return
        borrower = choice(other_accounts)
        pi = params.loan_payment_interval()
        txn = LoanSet(
            account=borrower.address,
            loan_broker_id=broker.loan_broker_id,
            counterparty=broker.owner,
            principal_requested=params.loan_principal(),
            interest_rate=params.loan_interest_rate(),
            payment_total=params.loan_payment_total(),
            payment_interval=pi,
            grace_period=params.loan_grace_period(pi),
        )
        # Deliberately skip co-signing — submit with only borrower's signature
        await submit_tx("LoanSet", txn, client, borrower.wallet)


# ── Loan Delete ──────────────────────────────────────────────────────


async def loan_delete(
    accounts: dict[str, UserAccount], loans: list[Loan], client: AsyncJsonRpcClient
) -> None:
    if params.should_send_faulty():
        return await _loan_delete_faulty(accounts, loans, client)
    return await _loan_delete_valid(accounts, loans, client)


async def _loan_delete_valid(
    accounts: dict[str, UserAccount], loans: list[Loan], client: AsyncJsonRpcClient
) -> None:
    if not loans:
        return
    # Prefer loans with zero principal — loans with debt return tecHAS_OBLIGATIONS
    paid_off = [ln for ln in loans if ln.principal <= 0 and ln.borrower in accounts]
    loan = choice(paid_off) if paid_off else choice(loans)
    if loan.borrower not in accounts:
        return
    borrower = accounts[loan.borrower]
    txn = LoanDelete(
        account=borrower.address,
        loan_id=loan.loan_id,
    )
    await submit_tx("LoanDelete", txn, client, borrower.wallet)


async def _loan_delete_faulty(
    accounts: dict[str, UserAccount], loans: list[Loan], client: AsyncJsonRpcClient
) -> None:
    if not accounts:
        return
    mutation = choice(["nonexistent_loan", "non_borrower"])

    if mutation == "nonexistent_loan":
        acc = choice(list(accounts.values()))
        txn = LoanDelete(
            account=acc.address,
            loan_id=params.fake_id(),
        )
        await submit_tx("LoanDelete", txn, client, acc.wallet)

    elif mutation == "non_borrower":
        if not loans:
            return
        loan = choice(loans)
        non_borrowers = [a for a in accounts.values() if a.address != loan.borrower]
        if not non_borrowers:
            return
        impostor = choice(non_borrowers)
        txn = LoanDelete(
            account=impostor.address,
            loan_id=loan.loan_id,
        )
        await submit_tx("LoanDelete", txn, client, impostor.wallet)


# ── Loan Manage ──────────────────────────────────────────────────────


async def loan_manage(
    accounts: dict[str, UserAccount],
    loan_brokers: list[LoanBroker],
    loans: list[Loan],
    client: AsyncJsonRpcClient,
) -> None:
    if params.should_send_faulty():
        return await _loan_manage_faulty(accounts, loan_brokers, loans, client)
    return await _loan_manage_valid(accounts, loan_brokers, loans, client)


def _state_aware_manage_flag(loan: Loan) -> LoanManageFlag:
    """Pick a contextually appropriate manage flag based on loan state."""
    if loan.is_defaulted:
        # Already defaulted — try impair/unimpair (likely errors, good for exploration)
        return choice([LoanManageFlag.TF_LOAN_IMPAIR, LoanManageFlag.TF_LOAN_UNIMPAIR])
    if loan.is_impaired:
        # Impaired — can unimpair or escalate to default
        return choice([LoanManageFlag.TF_LOAN_UNIMPAIR, LoanManageFlag.TF_LOAN_DEFAULT])
    # Normal — can impair or default
    return choice([LoanManageFlag.TF_LOAN_IMPAIR, LoanManageFlag.TF_LOAN_DEFAULT])


async def _loan_manage_valid(
    accounts: dict[str, UserAccount],
    loan_brokers: list[LoanBroker],
    loans: list[Loan],
    client: AsyncJsonRpcClient,
) -> None:
    if not loans or not loan_brokers:
        return
    loan = choice(loans)
    broker = next((b for b in loan_brokers if b.loan_broker_id == loan.loan_broker_id), None)
    if not broker or broker.owner not in accounts:
        return
    owner = accounts[broker.owner]
    txn = LoanManage(
        account=owner.address,
        loan_id=loan.loan_id,
        flags=_state_aware_manage_flag(loan),
    )
    await submit_tx("LoanManage", txn, client, owner.wallet)


async def _loan_manage_faulty(
    accounts: dict[str, UserAccount],
    loan_brokers: list[LoanBroker],
    loans: list[Loan],
    client: AsyncJsonRpcClient,
) -> None:
    if not accounts:
        return
    mutation = choice(["nonexistent_loan", "non_broker_owner"])

    if mutation == "nonexistent_loan":
        acc = choice(list(accounts.values()))
        flag = choice(
            [
                LoanManageFlag.TF_LOAN_DEFAULT,
                LoanManageFlag.TF_LOAN_IMPAIR,
                LoanManageFlag.TF_LOAN_UNIMPAIR,
            ]
        )
        txn = LoanManage(
            account=acc.address,
            loan_id=params.fake_id(),
            flags=flag,
        )
        await submit_tx("LoanManage", txn, client, acc.wallet)

    elif mutation == "non_broker_owner":
        if not loans:
            return
        loan = choice(loans)
        acc = choice(list(accounts.values()))
        flag = choice(
            [
                LoanManageFlag.TF_LOAN_DEFAULT,
                LoanManageFlag.TF_LOAN_IMPAIR,
                LoanManageFlag.TF_LOAN_UNIMPAIR,
            ]
        )
        txn = LoanManage(
            account=acc.address,
            loan_id=loan.loan_id,
            flags=flag,
        )
        await submit_tx("LoanManage", txn, client, acc.wallet)


# ── Loan Pay ─────────────────────────────────────────────────────────


async def loan_pay(
    accounts: dict[str, UserAccount], loans: list[Loan], client: AsyncJsonRpcClient
) -> None:
    if params.should_send_faulty():
        return await _loan_pay_faulty(accounts, loans, client)
    return await _loan_pay_valid(accounts, loans, client)


def _state_aware_pay_amount(loan: Loan) -> str:
    """Generate a pay amount informed by tracked loan principal."""
    if loan.principal <= 0:
        return params.loan_pay_amount()
    strategy = choice(["full", "installment", "random"])
    if strategy == "full":
        return str(loan.principal)
    elif strategy == "installment":
        return str(max(1, loan.principal // 3))
    return params.loan_pay_amount()


async def _loan_pay_valid(
    accounts: dict[str, UserAccount], loans: list[Loan], client: AsyncJsonRpcClient
) -> None:
    if not loans:
        return
    loan = choice(loans)
    if loan.borrower not in accounts:
        return
    borrower = accounts[loan.borrower]
    txn = LoanPay(
        account=borrower.address,
        loan_id=loan.loan_id,
        amount=_state_aware_pay_amount(loan),
    )
    await submit_tx("LoanPay", txn, client, borrower.wallet)


async def _loan_pay_faulty(
    accounts: dict[str, UserAccount], loans: list[Loan], client: AsyncJsonRpcClient
) -> None:
    if not accounts:
        return
    mutation = choice(["nonexistent_loan", "zero_payment"])

    if mutation == "nonexistent_loan":
        acc = choice(list(accounts.values()))
        txn = LoanPay(
            account=acc.address,
            loan_id=params.fake_id(),
            amount=params.loan_pay_amount(),
        )
        await submit_tx("LoanPay", txn, client, acc.wallet)

    elif mutation == "zero_payment":
        if not loans:
            return
        loan = choice(loans)
        if loan.borrower not in accounts:
            return
        borrower = accounts[loan.borrower]
        txn = LoanPay(
            account=borrower.address,
            loan_id=loan.loan_id,
            amount="0",
        )
        await submit_tx("LoanPay", txn, client, borrower.wallet)

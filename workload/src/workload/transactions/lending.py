"""Lending Protocol transaction generators for the antithesis workload.

Lending requires a vault to exist first, then a broker is created on that vault,
then loans are taken against the broker.
"""

from workload import logging, params
from workload.randoms import choice
from workload.submit import submit_tx
from xrpl.models.transactions import (
    LoanBrokerSet,
    LoanBrokerDelete,
    LoanBrokerCoverDeposit,
    LoanBrokerCoverWithdraw,
    LoanSet,
    LoanDelete,
    LoanManage,
    LoanPay,
)
from xrpl.models.transactions.loan_manage import LoanManageFlag

log = logging.getLogger(__name__)


# ── Loan Broker Set ──────────────────────────────────────────────────

async def loan_broker_set(accounts, vaults, loan_brokers, client):
    if params.should_send_faulty():
        return await _loan_broker_set_faulty(accounts, vaults, loan_brokers, client)
    return await _loan_broker_set_valid(accounts, vaults, loan_brokers, client)


async def _loan_broker_set_valid(accounts, vaults, loan_brokers, client):
    if not vaults:
        log.debug("No vaults for loan_broker_set")
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


async def _loan_broker_set_faulty(accounts, vaults, loan_brokers, client):
    pass  # TODO: fault injection


# ── Loan Broker Delete ───────────────────────────────────────────────

async def loan_broker_delete(accounts, loan_brokers, client):
    if params.should_send_faulty():
        return await _loan_broker_delete_faulty(accounts, loan_brokers, client)
    return await _loan_broker_delete_valid(accounts, loan_brokers, client)


async def _loan_broker_delete_valid(accounts, loan_brokers, client):
    if not loan_brokers:
        log.debug("No loan brokers to delete")
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


async def _loan_broker_delete_faulty(accounts, loan_brokers, client):
    pass  # TODO: fault injection


# ── Loan Broker Cover Deposit ────────────────────────────────────────

async def loan_broker_cover_deposit(accounts, loan_brokers, client):
    if params.should_send_faulty():
        return await _loan_broker_cover_deposit_faulty(accounts, loan_brokers, client)
    return await _loan_broker_cover_deposit_valid(accounts, loan_brokers, client)


async def _loan_broker_cover_deposit_valid(accounts, loan_brokers, client):
    if not loan_brokers:
        log.debug("No loan brokers for cover deposit")
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


async def _loan_broker_cover_deposit_faulty(accounts, loan_brokers, client):
    pass  # TODO: fault injection


# ── Loan Broker Cover Withdraw ───────────────────────────────────────

async def loan_broker_cover_withdraw(accounts, loan_brokers, client):
    if params.should_send_faulty():
        return await _loan_broker_cover_withdraw_faulty(accounts, loan_brokers, client)
    return await _loan_broker_cover_withdraw_valid(accounts, loan_brokers, client)


async def _loan_broker_cover_withdraw_valid(accounts, loan_brokers, client):
    if not loan_brokers:
        log.debug("No loan brokers for cover withdraw")
        return
    broker = choice(loan_brokers)
    if broker.owner not in accounts:
        return
    owner = accounts[broker.owner]
    txn = LoanBrokerCoverWithdraw(
        account=owner.address,
        loan_broker_id=broker.loan_broker_id,
        amount=params.loan_cover_deposit_amount(),
    )
    await submit_tx("LoanBrokerCoverWithdraw", txn, client, owner.wallet)


async def _loan_broker_cover_withdraw_faulty(accounts, loan_brokers, client):
    pass  # TODO: fault injection


# ── Loan Set ─────────────────────────────────────────────────────────

async def loan_set(accounts, loan_brokers, loans, client):
    if params.should_send_faulty():
        return await _loan_set_faulty(accounts, loan_brokers, loans, client)
    return await _loan_set_valid(accounts, loan_brokers, loans, client)


async def _loan_set_valid(accounts, loan_brokers, loans, client):
    if not loan_brokers:
        log.debug("No loan brokers for loan_set")
        return
    broker = choice(loan_brokers)
    other_accounts = [a for a in accounts if a != broker.owner]
    if not other_accounts:
        return
    borrower_id = choice(other_accounts)
    borrower = accounts[borrower_id]
    txn = LoanSet(
        account=borrower.address,
        loan_broker_id=broker.loan_broker_id,
        principal_requested=params.loan_principal(),
        interest_rate=params.loan_interest_rate(),
        payment_total=params.loan_payment_total(),
        payment_interval=(pi := params.loan_payment_interval()),
        grace_period=params.loan_grace_period(pi),
    )
    await submit_tx("LoanSet", txn, client, borrower.wallet)


async def _loan_set_faulty(accounts, loan_brokers, loans, client):
    pass  # TODO: fault injection


# ── Loan Delete ──────────────────────────────────────────────────────

async def loan_delete(accounts, loans, client):
    if params.should_send_faulty():
        return await _loan_delete_faulty(accounts, loans, client)
    return await _loan_delete_valid(accounts, loans, client)


async def _loan_delete_valid(accounts, loans, client):
    if not loans:
        log.debug("No loans to delete")
        return
    loan = choice(loans)
    if loan.borrower not in accounts:
        return
    borrower = accounts[loan.borrower]
    txn = LoanDelete(
        account=borrower.address,
        loan_id=loan.loan_id,
    )
    await submit_tx("LoanDelete", txn, client, borrower.wallet)


async def _loan_delete_faulty(accounts, loans, client):
    pass  # TODO: fault injection


# ── Loan Manage ──────────────────────────────────────────────────────

async def loan_manage(accounts, loan_brokers, loans, client):
    if params.should_send_faulty():
        return await _loan_manage_faulty(accounts, loan_brokers, loans, client)
    return await _loan_manage_valid(accounts, loan_brokers, loans, client)


async def _loan_manage_valid(accounts, loan_brokers, loans, client):
    if not loans or not loan_brokers:
        log.debug("No loans to manage")
        return
    loan = choice(loans)
    broker = next((b for b in loan_brokers if b.loan_broker_id == loan.loan_broker_id), None)
    if not broker or broker.owner not in accounts:
        return
    owner = accounts[broker.owner]
    flag = choice([LoanManageFlag.TF_LOAN_DEFAULT, LoanManageFlag.TF_LOAN_IMPAIR, LoanManageFlag.TF_LOAN_UNIMPAIR])
    txn = LoanManage(
        account=owner.address,
        loan_id=loan.loan_id,
        flags=flag,
    )
    await submit_tx("LoanManage", txn, client, owner.wallet)


async def _loan_manage_faulty(accounts, loan_brokers, loans, client):
    pass  # TODO: fault injection


# ── Loan Pay ─────────────────────────────────────────────────────────

async def loan_pay(accounts, loans, client):
    if params.should_send_faulty():
        return await _loan_pay_faulty(accounts, loans, client)
    return await _loan_pay_valid(accounts, loans, client)


async def _loan_pay_valid(accounts, loans, client):
    if not loans:
        log.debug("No loans to pay")
        return
    loan = choice(loans)
    if loan.borrower not in accounts:
        return
    borrower = accounts[loan.borrower]
    txn = LoanPay(
        account=borrower.address,
        loan_id=loan.loan_id,
        amount=params.loan_pay_amount(),
    )
    await submit_tx("LoanPay", txn, client, borrower.wallet)


async def _loan_pay_faulty(accounts, loans, client):
    pass  # TODO: fault injection

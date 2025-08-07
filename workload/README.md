# Workload for Antithesis

To start the workload

The container image Antithesis runs


| Layer                     | Responsibility                                                      | Example module                       |
| ------------------------- | ------------------------------------------------------------------- | ------------------------------------ |
| **domain**                | Pure data types, business entities, value objects. No side effects. | `WalletModel`, `IssuedCurrencyModel` |
| **infrastructure**        | External interactions (xrpl client, network I/O, storage).          | `Workload`, `Store`, `xrpl` bindings |
| **application / logic**   | Coordination and orchestration.                                     | `txn_factory`, `account_generator`   |
| **interface / CLI / API** | Entry points (FastAPI, Typer, etc.).                                | `main.py`, `cli.py`                  |


```python
domain.wallet.WalletModel
domain.currency.IssuedCurrencyModel
```

are stable data contracts used by everything else.
They stay simple, type-safe, and import-light, while `workload` and `txn_factory` evolve independently.


## TODO
- [ ] Once a txn is submitted, add it to subscritions. When the txn is seen. Send antithesis lifecycle event.
- [ ] Precommit linting and formatting with ruff.
- [ ] Move all the TODOs for default constants.
- [ ] Allow specifying which node you're sending a txn to.
- [ ] Integrate the sidecar into the workload.
- [ ] Don't need to wait N ledgers when we know it's running and ready (development.)
- [ ] API endpoint that modulates the amount of traffic.
- [ ] Overlapping UNLs (try to mess with consensus).
- [ ] Start the run with M of N validators runnings and bring them up during the run
- [ ] Package & CI.

## Research

What can this [Hypothesis](https://hypothesis.readthedocs.io/en/latest/) library be used for?

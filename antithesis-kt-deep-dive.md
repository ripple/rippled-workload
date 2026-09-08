# Antithesis KT — Deep Dive (Hitesh's four questions)

> Companion to `antithesis-kt-notes.md`. That doc covers *what Antithesis is*, *reading a report*, and *the live 403 blocker*. This one covers the four things Hitesh asked for: **repo responsibilities · integrating a workload · how the engine works · how fuzzing happens.**
>
> **Open these before you start:**
> Editor — `rippled-workload` with four tabs pre-opened: `.claude/rules/antithesis.md` · `CLAUDE.md` · `workload/src/workload/fuzz.py` · `test_composer/all_transactions/parallel_driver_escrow_create_random.sh`
> Browser — `rippled-antithesis` → `.github/workflows/start_experiment.yml`
>
> Read the plain text aloud. **▶ OPEN** lines are stage directions — don't read them out.

---

## 1. The two repos — who owns what

"There are actually three repos, and the split confuses everyone at first, so let me give you the one-line version of each.

`xrpld-private` is the thing being tested — rippled itself. We don't touch it, we just point at a branch. Right now that's `staging/3.3.x-private`.

`rippled-antithesis` is the ignition key. That's it. It's CI: it builds the container images, tags them with a run number, holds the webhook credentials, and makes one authenticated call to Antithesis saying 'launch a run with these images for this many hours.' It knows nothing about XRPL. If you're debugging a launch failure — like the 403 we're stuck on — that's the repo. If you're debugging *test content*, it's never the repo.

`rippled-workload` is everything that actually knows what XRPL is. And this is the part people get wrong: it's not just the transaction generator. It also defines the **network topology** and every rippled config file, via `prepare-workload/`. So the shape of the test network — how many validators, which ones get pruning, what amendments are on — is decided here, not in the antithesis repo. Plus the assertions, the sidecar that watches the validators, and the little driver scripts Antithesis runs.

The rule of thumb: **launch problems live in `rippled-antithesis`, everything else lives in `rippled-workload`.**"

> ▶ OPEN, in order: `prepare-workload/prepare_workload/templates/compose.yml.mako` (the network being generated) → `prepare-workload/prepare_workload/templates/xrpld.cfg.mako` (the node config being generated) → `prepare-workload/prepare_workload/amendments.py`. Then flip to the browser tab and show `start_experiment.yml` — point at `antithesis.images` in `WEBHOOK_DATA` and say "that's the entire handoff between the two repos."

---

## 2. How the engine works

"Antithesis isn't a fuzzer with a nice UI. The thing we're actually renting is a **deterministic hypervisor**.

Normally when you run a distributed system, thread scheduling, network timing, clock reads and randomness are all outside your control — that's why concurrency bugs are unreproducible. Antithesis runs the whole network inside a VM where all of that is controlled and recorded. Two things fall out of that.

First, **anything it finds is perfectly reproducible.** Same seed, same execution, byte for byte. Normal fuzzers find a bug and lose it; this hands us the exact timeline back.

Second, it can **snapshot a running universe and branch from it** — explore many different futures from the same moment instead of restarting from scratch. That's why it finds rare interleavings a normal test run never would. And the search isn't blind: it uses coverage feedback from instrumented binaries to steer toward code it hasn't reached.

On top of that it injects faults — network partitions, dropped and reordered packets, killing and restarting nodes, clock skew, slow and full disks.

Now the part that matters operationally: a run has **phases**, and our scripts are picked by filename prefix."

```
setup_complete() ──▶ [first_*] ──▶ [drivers + anytime_*] ──▶ [eventually_* / finally_*]
                     no faults      faults ACTIVE              faults stopped
```

"So: we bootstrap ledger state with no faults happening, we tell the platform 'setup is done', *then* it starts breaking things while our drivers hammer transactions, and at the end faults stop and we check the network recovered. That last phase is one script — `eventually_network_probe.sh` — and it just checks a payment can still validate after everything we did to it.

One design decision worth explaining: the workload submits to a **non-validating tracking node**, `xrpld`, which is deliberately shielded from fault injection. The five validators plus the fuzzer node take all the faults. If we submitted through a validator, fault-induced jitter would corrupt our transaction results and we couldn't tell a real bug from noise."

> ▶ OPEN: `.claude/rules/antithesis.md` — scroll to **Test composer phases** (the diagram is right there) then **Network topology**. This single file is the best artifact for this section.

---

## 3. How fuzzing takes place

"First, an important disambiguation, because there are **two completely different fuzzers** here and the names collide.

**One — transaction fuzzing.** That's ours, in this repo. We generate malformed XRPL transactions and submit them over RPC.

**Two — the `fuzzer` container.** That's a separate binary, `rippled-fuzzer`, built from its own commit — that's the `fuzzer` and `fuzzer_commit` inputs on the workflow. It runs *alongside* a real xrpld in the same container, and it aliases a loopback IP for every real peer in the network, so it can impersonate multiple peers at once and attack the **peer-to-peer protocol layer** rather than the transaction layer.

So: ours attacks the transaction path, the fuzzer container attacks the network path. Different surfaces, same run."

> ▶ OPEN: `fuzzer-entrypoint.sh` — 30 lines, and you can see both processes start and the loopback aliases being added. Best possible artifact for this point.

"Now our side. It's a ladder of four bands, each one reaching deeper.

**Band 1 — valid transactions.** Correct, well-formed. These matter because they build real ledger state — accounts, trust lines, tokens, vaults, NFTs — so the malformed ones have something real to act on, and so our invariants have something meaningful to check.

**Band 2 — curated faults.** Hand-written malformations per transaction type: fake object IDs, zero amounts, submitting as a non-owner, overdrawing. Each one is aimed at a specific error code we expect rippled to return.

**Band 3 — generative fuzzing.** This is the highest-yield band. We take a valid transaction, then apply one to three type-aware mutations to its fields — boundary integers, hostile hashes, oversized arrays, injecting fields that shouldn't be there. Critically it stays *encodable and signed*, so it survives the client library and actually reaches rippled's preflight, preclaim and apply logic. If it died at the codec we'd learn nothing.

**Band 4 — raw byte corruption.** A low-probability escalation that corrupts *through* the codec — encodings no honest client could ever produce. Truncated blobs, duplicated fields, non-canonical field ordering, lying about a length prefix, unregistered transaction type codes. This one deliberately targets rippled's C++ deserializer, which runs *before* signature checks, so a broken signature doesn't matter.

Two clever bits worth mentioning.

`assembler.py` is what makes band 4 safe. It can take a serialized transaction apart into fields and put it back byte-identically — so when we corrupt one field, that's provably the *only* thing that changed. There's a CI check that guards that property.

And all our randomness goes through Antithesis's own random source, not Python's. That's what makes replay work — the platform owns the dice, so it can rewind them."

> ▶ OPEN, in order: `workload/src/workload/fuzz.py` (band 3) → `rawfuzz.py` (band 4) → `assembler.py` (the safety primitive) → `randoms.py` (three lines, but it's the reason replay works).

---

## 4. How to integrate a workload

"Start with the punchline, because it demystifies the whole thing. Here is the *entire* Antithesis-side integration for one transaction type:"

```bash
#!/usr/bin/env bash

curl --silent http://workload:8000/escrow/create/random
```

"That's it. That's the file. Antithesis's job is to run that script over and over in parallel; all the intelligence lives behind the URL, in our server. We have about eighty of these, one per transaction type.

So 'adding a workload' really means adding an endpoint plus a two-line script. In practice it's a checklist of about ten steps, and the shape is: describe the random fields, write the transaction module with a valid and a faulty path, register it, add the driver script, then teach the CI checks about it.

The bit I'd emphasise: **you cannot half-add a type.** There are five gate scripts and they run in CI. One checks every module imports. One checks every endpoint is actually registered. One checks every faulty path wires up generative fuzzing. One checks every new type is classified against every submit-time modifier — you have to explicitly say 'this applies' or 'this doesn't, and here's why.' And one checks the assembler round-trip still holds. Miss a step and CI stops you rather than letting a silently-untested type into the suite.

The other thing to know is the **setup chain** — there's a strict dependency order, because you can't distribute a token before you've issued it, and you can't test permissioned DEX paths before credentials are accepted. If you add a type whose object other transactions depend on, you add a setup step too."

> ▶ OPEN, in order: `test_composer/all_transactions/parallel_driver_escrow_create_random.sh` (show it's genuinely two lines — do this first, it lands) → `CLAUDE.md`, jump to **"Add a transaction type"** (the ten steps) → `workload/src/workload/transactions/escrow_create.py` (the valid/faulty split) → `workload/src/workload/setup.py`, read the dependency-chain comment at the top → `scripts/` (the five gates).

---

## Questions he'll probably ask — short answers ready

**"Why a web server for this?"** So Antithesis's driver scripts can stay dumb. They just hit URLs; all the transaction-building smarts live in one place we control.

**"Why 23 hours?"** Rare interleavings need search time. Short runs (`duration=0.5`) are for validating that a change works at all; the nightly 23-hour run is where real findings come from.

**"Can we run this locally?"** Partly — `LOCAL_TESTING.md` covers running the network under plain docker-compose. What you *can't* reproduce locally is the deterministic hypervisor, the fault injection, or the search. Local is for "does my workload work", not "is rippled correct."

**"Why does the report show more findings than we have bugs?"** Antithesis force-enables **all** amendments, so it tests a superset of what the release actually ships. A finding can be real in the fuzzer and irrelevant to the release. Always reconcile against the RC's real amendment set.

**"Who owns a red result?"** Red `always` or `unreachable` = rippled bug. Red `sometimes` or a reachability miss = our coverage gap. (Full table is in `antithesis-kt-notes.md`.)

---

## Numbers to have handy

- **3 repos** — `xrpld-private` (under test) · `rippled-antithesis` (launch/CI) · `rippled-workload` (everything else)
- **4 images per run** — `xrpld` · `workload` · `sidecar` · `config`
- **~30** transaction modules · **~81** registered types · **~80** driver scripts
- **7 nodes** — 1 non-validating tracking node (`xrpld`, fault-shielded) + `val0`–`val4` + `fuzzer` (all fault-exposed)
- **4 fuzzing bands** — valid → curated faults → generative → raw bytes
- **5 CI gates** — imports · endpoints · fuzz coverage · modifier coverage · assembler round-trip

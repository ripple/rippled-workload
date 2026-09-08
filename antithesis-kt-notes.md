# Antithesis KT — Talk Track + Cockpit

> **Open these tabs before you start:**
> Browser — QE-796 (`https://ripplelabs.atlassian.net/browse/QE-796`) · Antithesis report (`https://ripple.antithesis.com/report`) · Run #1392 last success (`https://github.com/ripple/rippled-antithesis/actions/runs/30948117984`) · Run #1404 latest probe (`https://github.com/ripple/rippled-antithesis/actions/runs/31051766510`) · `rippled-antithesis` repo · Antithesis docs (`https://antithesis.com/docs/`).
> Editor — `CLAUDE.md` (anchor tab) in `rippled-workload`.
>
> Read the plain text aloud. The **▶ OPEN** lines are stage directions — don't read them out.

---

## What it is, in one breath
"Antithesis is a third-party deterministic-simulation testing platform — think of it as a fuzzing supercomputer we rent. We ship them `rippled` plus our own tooling as container images, and they run the node inside a fully controlled, reproducible universe. They throw randomized and deliberately-broken transactions at it, and they inject faults — network partitions, node crashes, clock skew. While that's happening, our assertions act as tripwires. If the node ever violates one, Antithesis catches the exact state and can replay the whole timeline deterministically. That reproducibility is the magic — normal fuzzers find a bug and lose it; Antithesis hands us the exact sequence back."
> ▶ OPEN: nothing (or the Antithesis docs tab).

## How a single run is put together
"Every run is really four container images stitched together. First, `xrpld` — the node under test, built from `xrpld-private`; for 3.3.0 we build off `staging/3.3.x-private`. Second, `workload` — our repo, `rippled-workload`, a FastAPI server that generates XRPL transactions. Third, `sidecar` — it watches the validators and emits observations, like whether an `online_delete` database rotation happened. And fourth, a `config` image describing the network topology and driver scripts. CI builds all four, tags them with the run number, and fires a webhook at Antithesis to launch. Antithesis boots the network from those images and runs the search for however many hours we ask — right now the goal is a 23-hour run."
> ▶ OPEN: `rippled-antithesis` → `.github/workflows/start_experiment.yml`. Point at the `antithesis.images` line in `WEBHOOK_DATA` — it lists `workload:… ; xrpld:… ; sidecar:…` plus `antithesis.config_image`. That's the four images made concrete.

## The workload generator — where we spend our time
"`rippled-workload` is the heart of it. It's a FastAPI app: each transaction type has an endpoint, and Antithesis's shell driver scripts just curl those endpoints over and over. Every type is registered in a `REGISTRY` and has two paths — a `valid` handler that builds a correct transaction, and a `faulty` handler that deliberately malforms it. The faulty side always includes a generative `fuzz` option plus hand-written fault vectors. There's also a setup chain that bootstraps ledger state in dependency order — accounts, trust lines, tokens, vaults, NFTs — before fuzzing starts, so transactions have real objects to act on."
> ▶ OPEN, in order: `transactions/__init__.py` (the `REGISTRY` 5-tuples) → **`transactions/mpt_dynamic.py`** (our running example — the `_valid`/`_faulty` split + shared `_mpt_issuance_set_dynamic_base()`) → `params.py` (every random field lives here) → `setup.py` (read the dependency-chain comment at the top).

> 🧩 **One running example for the whole KT — a "dynamic-MPT rule change."** Use this single transaction type for every question, so you never switch examples. Plain version: an **MPT** is a custom coin someone invents; a **dynamic** MPT lets its rules be changed after it's made ("can be traded / frozen / clawed back") — **unless** a rule was **laminated shut** at the start, in which case changing it bounces. *(For engineers: endpoint `/mpt/set/dynamic/random`, file `transactions/mpt_dynamic.py`.)*

### The workload generator — explained simply (talkable)

**What it is in one line:** "`rippled-workload` is a little web server whose only job is to churn out XRP transactions to throw at rippled during the test. 'FastAPI' is just the tool we built it with — like saying 'it's built in Excel'; it tells you what it's made of, not what it does."

**The analogy to use:**
> "Think of it as a **vending machine for transactions**. Each button makes a different kind of XRP transaction — a payment, an escrow, an NFT, or our running example a **dynamic-MPT rule change**. Antithesis walks up and mashes the buttons thousands of times to bombard the node with activity."

**How it actually works (one step deeper, still plain):**
- A **web server** just sits and waits for requests — like a website waiting for a click.
- Ours has a **list of URLs**, one per transaction type (e.g. `/payment/random`, `/escrow/create/random`).
- Antithesis's simple scripts just **"click" those URLs over and over** (with `curl`), and each click makes our server build and send one transaction to the node.
- Every button has **two modes**: a **valid** one (a correct transaction) and a **faulty** one (a deliberately broken one, to check the node rejects bad input safely).

**Why a web server at all:** "So Antithesis's dumb little scripts can drive it just by hitting URLs. The scripts stay simple; all the smarts — how to build each transaction — live in our server."

**One-liner for Hitesh:**
> "It's a transaction-generating web server. Antithesis pokes its URLs over and over, and each poke fires a real (or deliberately broken) XRP transaction at the node we're testing — e.g. hitting `/mpt/set/dynamic/random` fires a dynamic-MPT rule change."

## The two fuzzing layers
"We fuzz in two ways. The first stays a *legal-but-weird* message, so it reaches the node's real logic. The second *breaks the message on purpose*, past what any real wallet could send, to hammer the parser directly. With our example: the first flips a dynamic-MPT rule change to a huge or empty value but keeps it legal; the second takes the finished signed message and chops a byte off it."
> ▶ OPEN: `fuzz.py` (legal-but-weird) and `rawfuzz.py` (broken-on-purpose). `assembler.py` is the helper that keeps the second one safe.

## The assertions — this is the point
"Bugs are found by our assertions, in three flavors. `always` must hold on every branch of the simulation. `sometimes` means we expect to reach a state at least once — a coverage check that our fuzzing is exercising the interesting paths. And `unreachable` means we should never hit that line. When Antithesis reports a failure, it's one of these breaking — that's what we triage."
> ▶ OPEN: `assertions.py` (point at `always` / `sometimes` / `unreachable`). Mention `ws_listener.py` fires many of them from validated results.

### The assertions — explained simply (talkable)

**The plain-English version:** "Think of assertions as rules we hand the tester along with the software. Each rule is one of four kinds, and each kind answers a different question. Then Antithesis runs millions of scenarios and tells us which rules held and which broke."

**The four kinds — say it like this (with the dynamic-MPT running example):**
- **Always = 'this can never happen.'** "A promise the software must keep every single time — like 'money is never created out of thin air,' or for our example 'you can never change a rule that was laminated immutable at mint.' If it breaks even once, that's a real bug in rippled. This is what we actually care about."
- **Sometimes = 'this must happen at least once.'** "Not a bug in the software — a check on *us*. It proves our tester actually tried the interesting cases — e.g. 'sometimes a dynamic-MPT rule change should succeed' and 'sometimes one against a laminated rule should fail.' If the failure case never fires, it means we forgot to test failures, not that rippled is broken."
- **Reachable / 'seen' = 'did we even try this?'** "The most basic check — did we attempt this kind of transaction at all? If a whole feature was never touched, we can't claim it works."
- **Unreachable = 'this line should be impossible.'** "A tripwire on our own tooling. If our test harness itself hits a bug, we want it to fail loudly, not quietly hide a rippled problem."

**The one distinction Hitesh should walk away with:**
> "**Red 'always' = rippled has a bug.** **Red 'sometimes' = *we* have a testing gap.** One points at the product, the other points at us. That single rule tells you who owns every red result."

**Who owns a red — all four (say this):** "Red **Always** or red **Unreachable** = a real bug (the impossible happened); red **Sometimes** or red **Reachable** = our own gap (we never reached a case we meant to test)."

**Why we pre-register everything (the clever bit):** "At startup we tell Antithesis about every rule up front — even before we test it. That's what lets the report show 'this was never tested' as loudly as 'this was tested and failed.' Without it, an untested feature would just be invisible — and invisible is the most dangerous kind of gap."

**When each fires (the timeline):** "For every transaction we check three moments — *before* we send it (did we try it?), *right after* we send it (did the node choke immediately?), and *after the network agrees on it* (did it keep all its promises?). Three checkpoints, so any failure can be pinned to an exact moment we can replay."

**The honest carve-outs (shows rigor):** "A few transaction types *can't* succeed or *can't* fail in our test setup — for example, deleting an account always fails because every account owns something. We've explicitly listed those with a one-line reason each, so they don't raise false alarms. It's documented, not swept under the rug."
> ▶ OPEN: `assertions.py` — scroll to `tx_submitting` (~line 572), `tx_submitted` (~603), `tx_result` (~615) and say "these are the three checkpoints." Point at `_NO_SUCCESS_TYPES` / `_NO_FAILURE_TYPES` (~lines 86–106) as the documented carve-outs.

## How we launch and read results
"Launching is a GitHub Actions workflow in `rippled-antithesis` called `start_experiment` — it builds the images, then does an authenticated curl to the Antithesis webhook with the image tags, duration, and callback info. Results come back as an email report and a Slack post. For deeper digging there's a helper skill in the repo that pulls a run's failing assertions and full event stream from Antithesis's REST API."
> ▶ OPEN: in `start_experiment.yml`, the `curl -u "$WEBHOOK_USERNAME:$WEBHOOK_PASSWORD" … $WEBHOOK_URL` line (`WEBHOOK_URL = https://ripple.antithesis.com/api/v1/launch_experiment/ripple`). Then `.claude/skills/fetch-antithesis-results/SKILL.md`. Mention `start_multiverse_debugging.yml` for debug sessions.

## Reading the report — what "green" means
"Once a run finishes, the report at `ripple.antithesis.com/report` is where we judge it. I read it in three passes."
> ▶ OPEN: `https://ripple.antithesis.com/report`, pick the latest run.

**Pass 1 — did the run even come up?** "First I check the environment and setup were healthy — the network booted and our setup chain seeded state. If the system never came up cleanly, the whole run is invalid and nothing below it matters. That has to be green before I trust anything else."

**Pass 2 — the Properties section (our assertions).** "Every assertion shows up as a property. `always` and `unreachable` must be green — a red one is a genuine rippled invariant violation. `sometimes` must also be green, but a red one means our workload never reached that path — a coverage gap on our side, not a node bug. Quick rule: all colors green; red `always`/`unreachable` = rippled bug, red `sometimes` = our coverage gap."

**Pass 3 — the Findings section.** "Findings is Antithesis's list of actual isolated problems — crashes, invariant violations, health-check failures. Zero findings = healthy. Any finding = something breaking; each has a title, severity, and a link to reproduce it deterministically. I click in, read the failing assertion and event stream, and decide: real rippled bug, or workload/setup issue."
> ▶ OPEN: Properties tab, then Findings. Empty Findings = your green.

## The six property categories (what the report groups assertions under)

| Category | What it checks | Must be green? | If RED, whose problem? |
|---|---|---|---|
| **Setup** | System-under-test booted and our setup chain seeded state. | **Yes — non-negotiable** | **Ours.** Config/images/setup failed; the whole run is invalid. |
| **Test Efficiency** | Are we exploring effectively / reaching interesting states (`sometimes` / reachability). | Should be green | **Ours.** Workload/generators aren't exercising a path — a coverage gap. |
| **Performance** | Latency / throughput / resource properties. | Should be green | Usually **rippled** (a perf regression), occasionally environment. |
| **Correctness** | The real invariant checks — our `always` / `unreachable` about rippled. | **Yes** | **Rippled.** The money category — red = genuine node bug. |
| **Antithesis SDK** | Baseline health of the SDK integration — assertions are registered and evaluated. | **Yes** | **Ours.** SDK wiring/instrumentation issue. |
| **Antithesis Test Composer** | Our driver scripts (`test_composer/parallel_driver_*.sh`) ran without command failures. | **Yes** | **Ours.** A driver script/curl is failing — not the node. |

**One-line rule:** "Setup, Antithesis SDK, and Test Composer must always be green — red there means our harness, not rippled, and the run may be invalid. Test Efficiency green means our fuzzing reached the interesting states. Correctness (and Performance) is where a red result is a real rippled finding. All six green = clean run."

## Credentials and who owns the relationship
"Webhook credentials live in HashiCorp Vault under `rippled-antithesis/data/webhook`, and CI pulls them at runtime with Kubernetes auth — nothing hardcoded. We recently migrated from a shared `ripple` username to a per-key credential, `key_PCvfldVL`, stored as a GitHub secret. Bart and Xun own the Antithesis account; our contact on their team in Slack is Thomas Bolger."
> ▶ OPEN: the `hashicorp/vault-action` step in `start_experiment.yml`. Don't open Vault itself.

## Where we are right now — the live blocker (QE-796)
"Since August 4th, every launch returns a 403 Forbidden. I isolated it, and it's on Antithesis's side, not ours — same 403 with correct creds, wrong creds, and even no creds, across every endpoint including their own public API spec, from two different networks. A bad password would give a 401; we get a flat 403 on the whole API surface, which points to a tenant-level or edge block, most likely from their per-key migration. The only change on our side was the username swap, and I've proven that's not the cause. It's escalated to Thomas in Slack, tracked in QE-796 (Blocked), and I have a 30-second diagnostic ready to re-verify the second they flip something."
> ▶ OPEN three tabs side by side: (1) QE-796 diagnosis comment; (2) Run #1392 launch-step log showing `< HTTP/2 200`; (3) Run #1404 `webhook-probe` job log showing the wall of `HTTP_CODE=403`. The 200-then-all-403 contrast is the whole story.

## 🧠 Deep Dive — Hitesh's 4 questions (plain answers)

> Hitesh asked four things: **(1) what each repo does, (2) how we add a workload, (3) how the engine works, (4) how fuzzing works.** Answer each in one or two sentences below. **One running example ties them all together — a "dynamic-MPT rule change."** The `▶ OPEN` lines are just where to click if he wants to see code; don't read them aloud.

**The running example, in one line:** an MPT is a custom coin someone invents. A *dynamic* MPT lets its rules be changed after it's made ("can be traded / frozen / clawed back") — **unless** a rule was **laminated shut** at the start, in which case changing it bounces.

---

### 1. What each repo does

Three repos, three jobs — like testing a toy robot: one kid builds obstacle courses, one kid runs the testing machine, and the robot is the thing being tested.

| Repo | Its job | In one line |
|---|---|---|
| **`rippled-workload`** (this one) | **What to test with** | Builds the transactions, seeds the ledger, defines the rules (assertions), does the fuzzing. |
| **`rippled-antithesis`** | **How to run it** | The CI launcher — packages everything into images and presses "GO" at Antithesis. |
| **`XRPLF/xrpld-private`** | **The thing under test** | `rippled` itself; for 3.3.0 we build the `staging/3.3.x-private` branch. |

> ▶ OPEN: `rippled-antithesis/.github/workflows/start_experiment.yml` (the launcher) and this repo's `workload/` folder (the brains).

---

### 2. How we add a workload (a new transaction type)

It's a fixed checklist, and CI blocks you if you skip a step — like a vending machine where you can't sell a new button until it's fully wired and labelled.

**With our example:** to add the dynamic-MPT rule change, you write its fields, write a **valid** version (a correct rule change) and a **faulty** version (a broken one), register it in one list, and add a small script that calls it. Run the `check-*` scripts; if they pass, the new URL (`/mpt/set/dynamic/random`) turns on by itself. Every type follows this same shape.

**The one manager takeaway:** every type must have a valid path *and* a faulty path, and the faulty path must include fuzzing. **CI won't let you half-add a type** — that's what keeps coverage honest.

> ▶ OPEN: `transactions/mpt_dynamic.py` (our example — the valid vs. faulty split) → `transactions/__init__.py` (the registry list) → `scripts/check-endpoints`.

---

### 3. How the engine works (Antithesis)

It's a **video game with a perfect rewind button**, played by a robot. It runs the network millions of times while doing nasty things — crashing nodes, cutting the network, skewing the clock — and because every run is fully recorded, the instant a rule breaks it can rewind to the exact moment and show you how. A normal fuzzer breaks the toy but forgets how; Antithesis always remembers.

**Why that matters for us:** because it controls *all* randomness, any bug it finds is reproducible on demand — no "it flaked once." (That's also why our code pulls randomness from Antithesis, not plain Python — so our choices replay too.)

> ▶ OPEN: `assertions.py` (our rules) and `randoms.py` (why we use their randomness).

---

### 4. How fuzzing works

Two flavours, easy to picture with Lego:
- **Legal-but-weird (Band A):** hand the toy a *real but strange* Lego piece — a giant brick, a zero-size brick. With our example: a rule change with a huge or empty value, but still a legal message, so it reaches rippled's real logic.
- **Broken-on-purpose (Band B):** hand it a **snapped piece the factory could never make** — take the finished signed message and chop a byte off. That's aimed at rippled's parser, before it even checks signatures.

**The guarantee:** every faulty path is *required* to include the legal-but-weird fuzzer, and CI enforces it. So fuzzing isn't optional per type — it's built into all of them.

> ▶ OPEN: `fuzz.py` (Band A) and `rawfuzz.py` (Band B). `assembler.py` is the helper that keeps Band B safe.

---

### 🎤 The 30-second opener (say this)
> "Three repos: **rippled-workload** is the brains — it builds transactions, seeds state, defines the rules, and fuzzes. **rippled-antithesis** is the launcher that packages everything and starts a run. And **rippled** is the node under test. Antithesis runs it all in a simulator that can rewind any bug exactly. Adding a transaction is a fixed checklist CI enforces, and fuzzing is built into every one. I'll use one example throughout — a dynamic-MPT rule change — because it touches every part."

### 🧒 The same thing to a 10-year-old
> "We're testing the software that moves XRP. One team invents fake money-moves — some normal, some broken on purpose, like a coin whose rules you try to change even though they were locked shut. Another team drops it all into a giant robot that plays the game millions of times while yanking cables and crashing things. We give the robot rules like *'money can never appear from nowhere.'* If it ever breaks one, it rewinds to the exact second and shows us how — so we hand the bug straight to the engineers."

---

## If you want to go deeper later
"The single best reference is `CLAUDE.md` in the workload repo — extremely detailed, documents every subsystem, the transaction-adding checklist, and all the gotchas. Repos are `rippled-workload`, `rippled-antithesis`, and `xrpld-private`. Work is tracked in the QE Jira project. A set of `check-` scripts gate CI — imports, endpoints, fuzz coverage, modifier coverage — so if you add a type and skip a step, CI stops you."
> ▶ OPEN: `CLAUDE.md`, `docs/transaction-modifiers.md`, and the `scripts/` folder.

---

## Quick-reference facts (keep visible)
- **Repos:** `ripple/rippled-workload` (generator) · `ripple/rippled-antithesis` (launch/CI + config) · `XRPLF/xrpld-private` (node, branch `staging/3.3.x-private`).
- **Webhook:** `POST https://ripple.antithesis.com/api/v1/launch_experiment/ripple`
- **Report:** `https://ripple.antithesis.com/report`
- **Vault:** `rippled-antithesis/data/webhook`, key `password`; username `key_PCvfldVL`.
- **Runs:** #1392 = last success (`30948117984`); #1404 = latest probe (`31051766510`).
- **People:** Bart (bthomee) + Xun own the account; Thomas Bolger = Antithesis contact.
- **Jira:** QE-796 (Blocked).

## Green rules at a glance
- **Pass condition:** Setup/health green → all six property categories green → Findings empty.
- **Red triage:** `always`/`unreachable` or a crash = **rippled bug**; `sometimes` / Test Efficiency = **our coverage/properties gap**; Setup / SDK / Test Composer = **our config/images/harness**.

---

*Caveat: exact UI labels in the Antithesis report can shift between versions — the concepts (baseline health → assertion results → bug list) are what matter. Our own `always`/`sometimes` assertions mostly appear under **Correctness** and **Test Efficiency**.*

---

## 🔬 Deep Dive — the 403 blocker (QE-796), for the technically curious

> Read this section only if Hitesh wants the full engineering picture of *why* we're stuck. Everything above is enough for the KT itself; this is the "what exactly is broken and how do we know it's not us" appendix.

### The symptom
Since **August 4th**, every launch attempt against the Antithesis webhook returns **HTTP 403 Forbidden**. Before that, the identical workflow returned `HTTP/2 200` (run **#1392**, `30948117984` — our last clean launch). Nothing in our launch path changed except a credential migration (below), and we've proven that migration is not the cause.

### What actually happens on the wire
- **Request:** `POST https://ripple.antithesis.com/api/v1/launch_experiment/ripple`, authenticated with HTTP Basic auth (`curl -u "$WEBHOOK_USERNAME:$WEBHOOK_PASSWORD"`).
- **Response:** `403 Forbidden`, an **empty `text/plain` body**, and a **CSP header naming `ripple.antithesis.com`**. That header shape is an **edge/proxy** response, not the application answering — i.e. the request is being rejected *before* it reaches the launch API.

### Why we're confident it's their side, not ours
This is the core of the diagnosis. We ran a dedicated `webhook-probe` job (runs **#1404** `31051766510`, re-verified in **#1410** with the new key + Vault-injected password) that fired the same request under different credential conditions across **all 9 API routes**:

| Test | Credentials sent | Expected if *our* creds were the problem | What we actually got |
|---|---|---|---|
| Valid new key + real password | `key_PCvfldVL` + Vault password | `200` | **403** |
| Wrong password | valid user, bad password | **401 Unauthorized** | **403** |
| No credentials at all | none | 401/403 | **403** |

**The tell:** a credential problem produces a **401**. We get a **flat 403 on every route regardless of credentials** — including with the *correct* new key and real password. A system that rejects good creds, bad creds, and no creds identically, at the edge, is doing a **tenant-level or IP/edge block**, not authentication. Our side is demonstrably sending the right key (`key_PCvfldVL`) and a real Vault password (logs show `WEBHOOK_USERNAME: key_PCvfldVL`, `WEBHOOK_PASSWORD: ***`).

### The one change on our side — and why it's ruled out
The only thing we changed was the **credential migration**: from the shared `ripple` username to a per-key credential `key_PCvfldVL` (GitHub secret + Vault at `rippled-antithesis/data/webhook`). If that swap were the cause, the authenticated probe would return **401**, not 403 — and it wouldn't 403 the *no-credential* request the same way. It most likely coincides with **their** per-key migration on the tenant/edge, which is theirs to confirm.

### The lead we handed them
Our CI runner's **egress IP is `18.213.16.214`**. If they added an allowlist as part of the migration, that IP not being on it would produce exactly this edge-level 403. That's the single most actionable thing for their infra to check.

### What "fixed" looks like (the definitive test)
The only authoritative proof is a **`200`/2xx on an authenticated `POST .../launch_experiment/ripple`**. Until we see that, "blocked on their side" is a strong, evidence-backed inference — not a certainty about their internal cause. We have a ~30-second `webhook-probe` re-run ready to confirm the instant they flip something.

### Status & ownership
- **Tracked:** QE-796 (**Blocked**).
- **Escalated:** Thomas Bolger (Antithesis) in Slack, with the evidence above (200-then-all-403 contrast, egress IP, credential-matrix results).
- **Account owners on our side:** Bart (bthomee) + Xun.

> ▶ OPEN three tabs to tell the story visually: (1) Run #1392 launch-step log showing `< HTTP/2 200`; (2) Run #1404 / #1410 `webhook-probe` log showing the wall of `HTTP_CODE=403` across all routes and all credential conditions; (3) QE-796. The 200-then-uniform-403 contrast *is* the whole diagnosis.
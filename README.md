# UPI Autopay Mandate Recovery Agent

An explainable AI agent that recovers failed UPI Autopay mandate debits — diagnosing why each one failed, choosing a bounded intervention, and producing an auditable rationale for every action.

**The claim isn't "we recover more." It's that every action is explainable, bounded by RBI mandate rules, and traceable end to end — and that a recovery is only counted when a signature-verified webhook says the money actually arrived.**

### Live: [mandate-recovery-agent.onrender.com](https://mandate-recovery-agent.onrender.com)

The audit console runs there against real Razorpay test-mode infrastructure. A ₹9,999 recovery on it was confirmed by a webhook Razorpay actually signed and delivered — not a simulated event. `POST /demo/run-batch?limit=3` runs a live batch; the free instance sleeps when idle, so the first request takes ~30s to wake.

---

## Why UPI Autopay specifically

Card-mandate recovery is a mature product category. UPI Autopay is not the same problem:

- **Failure rates are structurally higher** — UPI Autopay depends on a real-time bank approval, so first-attempt success runs far below card mandates.
- **The failure codes are rail-specific.** `IE` (funds blocked against another mandate), `VA` (mandate revoked), `QA` (mandate paused by user), `MA0` (mandate not present) have no card-rail equivalent. A generic "failed payment" abstraction cannot reason about them.
- **RBI rules constrain the recovery itself** — a 24-hour pre-debit notice, revocable vs non-revocable mandate state, and retry caps are compliance boundaries, not retry heuristics.

The guardrail layer here is built from those specific rules. It is not a generic safety wrapper.

---

## Architecture

```
Failed mandate event  →  AI reasoning agent  →  Guardrail check  →  Action executor  →  Audit log
  (amount, NPCI            (diagnoses cause,      (RBI rules,         (Razorpay          (immutable,
   response code,           picks action,          retry caps,         Payment Link)      one row per
   retry history)           states confidence)     exposure cap)                          decision)
                                                         ↓                                    ↑
                                                   can override                          webhook flips
                                                   the AI entirely                    outcome → recovered
```

The critical property: **the guardrail is independent of the model.** It reads authoritative mandate state, not the AI's diagnosis, so it catches an unsafe retry even when the model misdiagnoses the failure completely.

---

## Quickstart

```bash
pip install -r requirements.txt
cp .env.example .env          # defaults to the offline provider — no API key needed
python run_batch.py           # full batch + metrics report
python -m pytest              # 90 tests
```

Four reasoning providers, switched with one env var. Every one of them forces
the decision through a declared function schema, so a malformed or prose
response is impossible by construction rather than merely discouraged:

| `REASONING_PROVIDER` | Backend | Cost |
|---|---|---|
| `groq` | Groq, OpenAI-compatible tool calling | free tier |
| `gemini` | Google AI Studio | free tier, 20 req/model/day |
| `anthropic` | Claude, native tool use | ~$0.14 per 60-case batch |
| `stub` | Deterministic offline fixture | none |

Running the same batch across providers is the point: the guardrail layer is
provider-agnostic, and it catches unsafe recommendations from all of them.

Stub output is tagged `[OFFLINE STUB]` in every audit row and the report prints a warning — fixture numbers can never be mistaken for real metrics.

```bash
python run_batch.py --limit 10    # small run
python run_batch.py --resume      # continue after a quota/rate-limit interruption
```

---

## The guardrail layer

Deterministic Python, unit-tested, and it overrides the model regardless of what it recommends:

| Rule | Behaviour |
|---|---|
| **Mandate state** | A revoked or paused mandate cannot be debited — retry is blocked and rerouted, however confident the AI is |
| **Retry cap** | Hard stop at 3 attempts |
| **24h pre-debit notice** | No `retry_now` executes unless the notice window is satisfied |
| **Daily exposure ceiling** | ₹ and attempt caps per customer per day, enforced *across* a batch |
| **Confidence routing** | Below 0.6 self-reported confidence, the case goes to human review |
| **Kill switch** | One env var halts every external side effect |

Every override writes a human-readable reason into the audit log:

```
MID9F213ABB63: mandate_revoked: a revoked mandate cannot be debited, so retry is
not technically possible; routing to notify_customer/escalate_human instead
```

---

## Audit trail

One immutable JSONL row per decision, carrying the input signal, the model's full reasoning, the guardrail verdict, the action taken, **the Payment Link actually created**, and the outcome.

`outcome: recovered` is set by exactly one thing: a signature-verified `payment_link.paid` webhook. Never by an API call succeeding. That is what makes the ₹-recovered figure defensible rather than self-reported.

Duplicate webhook deliveries are idempotent — Razorpay's docs warn they happen, and a double-count would corrupt the headline number.

---

## Honest metrics

`run_batch.py` reports diagnosis accuracy per cause, hard cases explicitly (never averaged away), guardrail overrides, escalation count, ₹ at risk vs recovered, and **false-positive cost** — cases where the model was confident and wrong.

### Measured run — 60 cases, Groq `openai/gpt-oss-120b`

| Metric | Value |
|---|---|
| Cause diagnosis accuracy | **97%** (58/60) |
| Guardrail overrides | **5** |
| Escalated to human review | **4** |
| ₹ at risk across the batch | **₹274,640** |
| False-positive cost | **₹12,998** (2 confident-but-wrong cases) |

Errors concentrate on the genuinely ambiguous codes — `B3` ("transaction not permitted to the account") and `QA` (mandate paused by user) — not on the common failure modes, which score 100%.

### The finding that matters

Accuracy is the least interesting number here. This is the important one, measured across two independent 60-case runs:

| Run | Accuracy | Confidence when **right** | Confidence when **wrong** |
|---|---|---|---|
| A | 93% | 0.898 | 0.895 |
| B | 97% | 0.889 | **0.940** |

In run A the two were indistinguishable. In run B the model was **more confident about its mistakes than its correct answers**.

The precise gap is noisy — with only 2–4 wrong cases per run, that statistic carries little weight on its own, and it would be dishonest to quote either figure as a stable measurement. What survives across both runs is the robust claim: **self-reported confidence does not reliably separate correct answers from incorrect ones.** It never behaved like a probability in either direction.

That is what "`confidence` is self-report, not a calibrated probability" means in practice, and it is the entire justification for this system's architecture. A recovery agent that trusted that number would route on noise. Instead:

- Confidence is used **only** as a one-way escalation trigger, never as permission to act.
- The compliance rules are deterministic code that override the model regardless of how certain it claims to be.
- The guardrail reads authoritative mandate state, not the model's diagnosis — so a confident misdiagnosis still cannot produce an illegal retry.

The threshold does earn its place occasionally: `ZA` ("transaction declined", no reason given) came back at **0.30** and was routed to a human. That is the signal working as intended — but on a handful of cases in 60, it is a backstop, not a control.

In production this signal would route through a calibrated classifier. The honest version of the claim is that the guardrails are load-bearing and the confidence score is not.

### The number that only moves on proof

This happened in two acts, and the first one matters as much as the second.

**Act one — the refusal.** A test-mode Payment Link for ₹9,999 was created by the executor and genuinely paid. Razorpay confirmed `status=paid`. The audit log still read **`pending`**.

That was not a bug. No signature-verified `payment_link.paid` event had arrived — the development network blocks inbound tunnelling, so Razorpay could not reach the local instance. The money was real and the agent still refused to count it. Marking it recovered would have meant trusting an API poll or the executor's own success, which is precisely the self-reported number this project exists to avoid.

**Act two — the proof.** Once deployed to a public endpoint, the same flow ran again. Razorpay's servers delivered a real signed webhook, the app verified the HMAC, resolved `MID21C56811CD-a0-2b801868` back to its mandate, and flipped the outcome:

```
Razorpay      plink_TYKDtjsrSokjEX   status = paid        INR 9,999
Audit log     MID21C56811CD          outcome = recovered  INR 9,999
```

Nothing in that chain was simulated: real model reasoning, a real Payment Link created by the deployed service, a real payment, and a webhook signed by Razorpay and verified on arrival.

The contrast is the point. The same system, the same real money — `pending` without proof, `recovered` with it. The ₹-recovered figure moves only on cryptographic evidence, and it under-reports rather than overstates when that evidence is missing.

### What the guardrails actually caught

Three distinct rules fired on real cases, none of them contrived:

```
UT   daily_exposure_cap_exceeded: retrying ₹9,999 would take this customer's
     same-day retry exposure above the ₹5,000 cap
U67  retry_cap_exceeded: 3 attempts already made, max is 3
ZA   low_confidence_escalation: AI confidence 0.30 is below the 0.60 threshold
```

**Reported honestly:** the mandate-state rule did *not* fire in this run — the model never recommended retrying a revoked mandate, so the block had nothing to catch. That case is covered by an explicit test rather than left to chance (`tests/test_guardrails.py`), including one asserting the block still holds when the model misdiagnoses the cause entirely.

---

## Engineering problems faced, and how they were solved

Each of these was found by testing against the real thing rather than by reading code.

### 1. The decline codes were fabricated

The first dataset used codes like `RC01`, `RC51`, `MD21` — plausible-looking, but **not real UPI codes**. They were card-style ISO 8583 codes with invented prefixes, which any payments engineer would spot instantly.

**Fixed by** verifying every code against Axis Bank's published *UPI Response Codes for H2H/API* list and replacing the full mapping: `Z9`, `IE`, `U67`, `UT`, `XY`, `IR`, `U28`, `Z8`, `Z7`, `ZU`, `M2`, `VA`, `QA`, `MA0`, `ZM`, `Z6`, `AM`, `ZA`, `U30`, `B3`. The `unknown` bucket now holds genuinely ambiguous codes (`ZA`, `U30` — declines the bank returns with no stated reason), so those cases cannot be solved by any lookup table.

### 2. The guardrail was reading the answer key

The mandate-state check tested `true_cause` — a ground-truth label that exists only for scoring. Functionally correct, but indefensible: a reviewer would call it cheating.

**Fixed by** adding a `mandate_status` field representing what an authoritative bank/NPCI mandate lookup returns. The guardrail reads that; the model never sees it. This is strictly stronger — the guardrail now blocks unsafe retries *even when the model's diagnosis is wrong*, which is covered by its own test.

### 3. The audit log discarded what actually happened

The executor's response — including the Payment Link id and URL — was computed and thrown away. The trail stopped at "we decided to notify," with no way to tie a decision to the link it produced. A hole in the project's headline feature.

**Fixed by** recording an `execution_result` on every audit row, filtered to audit-relevant fields so customer contact details from the provider response never enter the log.

### 4. The demo worked exactly once

Razorpay requires `reference_id` to be unique. It was set to the mandate id, so the **second** run of any mandate failed with *"payment link with given reference_id already exists."* Conceptually wrong too — a retry sequencer makes multiple attempts per mandate by design.

**Fixed by** making the reference per-attempt (`MID…-a2-0d95f7e4`) and resolving the mandate back from `notes.mandate_id`, with the reference prefix as fallback. Verified by running identical mandates twice against the live API.

### 5. The test suite was calling the live API

`load_dotenv()` runs at import, so once real credentials were in `.env`, **every `pytest` run created real Payment Links** on the account and burned the API rate limit. Test-fixture mandate ids were visible in the live dashboard.

**Fixed by** a session-wide `conftest.py` fixture that strips credentials, plus a regression test that fails loudly if anything tries to build a live client. Suite runtime dropped from 3.2s to 0.94s — confirmation the calls were real.

### 6. Rate limits were silently dropping cases

Razorpay returns throttling as an ordinary `BadRequestError`, and the SDK's built-in retry only covers `ConnectionError` — so a 40-link batch lost cases with no error surfaced. The same applied to the free-tier LLM (HTTP 429) and to network timeouts on a consumer connection.

**Fixed by** exponential backoff on both clients, distinguishing retryable throttling from genuine bad requests (which must fail fast), and a `--resume` flag so a quota-interrupted batch continues instead of re-spending on decided cases.

### 7. Tunnelling services were blocked by the network

Webhook testing needs a public URL for localhost. Both ngrok and Cloudflare Tunnel were unreachable — connection reset at the ISP, while GitHub and PyPI resolved normally.

**Fixed by** diagnosing per-host reachability rather than assuming general network failure, and switching to an SSH reverse tunnel (`localhost.run`), which was reachable.

### 8. The "reproducible" dataset wasn't reproducible

The generator takes a `--seed`, and the distribution was stable — but `mandate_id` came from `uuid4()`, which ignores the seed entirely. The same seed produced different mandate ids on every run.

This defeated the whole point of shipping a generator instead of a CSV: regenerating the batch live to show it isn't cherry-picked would produce ids that correlate with nothing. It also silently broke `--resume` and made any audit log impossible to tie back to the batch that produced it.

**Fixed by** drawing the id from the seeded RNG, and making the clock injectable so a given seed plus a given `now` yields a byte-identical batch. Two tests now pin this: full determinism under a fixed clock, and id stability regardless of wall-clock time.

### 9. The model was hallucinating the code book

With only the raw code as input, diagnosis accuracy was **42%**, and the failures were concentrated entirely on codes the model had never learned: `IE` was read as "Invalid Entity" (it means funds blocked against another mandate), `IR` as a mandate problem. Worse, it was *confident* while wrong — mean confidence 0.92, and not one case fell below the 0.6 escalation threshold, so the human-review path never fired.

The instinct is to call this an honest measure of model limits. It isn't: no production recovery system withholds its own reference table, so this was measuring recall of an obscure lookup rather than reasoning.

**Fixed by** supplying the verified NPCI code reference in the system prompt — the banks' own descriptions, deliberately **not** the cause labels. Mapping a description onto one of six causes, and then choosing a bounded intervention from retry history, amount, notice window and mandate type, remains the model's judgement. `tests/test_npci_codes.py` pins that line: it fails if any description ever contains a cause label, and if the ambiguous codes (`ZA`, `U30`) ever gain a diagnostic description.

Accuracy across the full 60-case batch went from **42% to 93%**, and the escalation route began firing on genuinely unresolvable codes instead of never firing at all.

What the code book did *not* fix is calibration: the model remained equally confident when wrong as when right (0.895 vs 0.898 — see the metrics section). Better inputs bought accuracy, not self-knowledge. That distinction is why the guardrails stayed deterministic rather than being relaxed once the numbers improved.

### 10. One malformed response killed a 60-case run

Thirty-six cases into a full batch, the provider returned `HTTP 400 tool_use_failed`. The model had generated `"confidence": 0. nine` — a number written as words, which is not valid JSON.

Two things are worth separating here. The **schema guarantee worked**: the malformed arguments were rejected at the provider and never reached the pipeline, which is exactly what forcing the tool call is for. The **handling was wrong**: the exception propagated all the way up and destroyed the run, losing the remaining 24 cases.

A recovery system that dies because one model response came back garbled is not production-ready — and the irony of an agent built around graceful degradation crashing on a bad payload is not lost.

**Fixed on two levels.** `tool_use_failed` is now retried, since it is a sampling glitch rather than a bad request. More importantly, a case the model cannot decide — for any reason, including provider outages and timeouts — becomes a **zero-confidence decision routed to human review** and recorded in the audit log, instead of an exception. The batch continues; the failure itself is auditable. Two tests cover it, including one asserting a batch runs past a failing case.

Combined with `--resume`, the interrupted run continued from case 37 without re-spending on the 36 already decided.

### 11. Python 3.8 blocked the official SDK

The free-tier provider's SDK requires Python 3.9+, and `pip` downloads were timing out.

**Fixed by** calling the REST API directly with `httpx` — already a dependency. This also gave direct control over `tool_config: {mode: "ANY"}`, which *forces* the function call, preserving the guarantee that a malformed or prose response is impossible by construction.

---

## Project structure

```
agent/
  schemas.py          pydantic models — validation, storage and export share one definition
  reasoning_agent.py  provider switch + Claude forced tool-calling
  groq_agent.py       free-tier provider over REST, forced tool calling
  gemini_agent.py     free-tier provider over REST
  npci_codes.py       verified NPCI/UPI code reference given to the model
  stub_agent.py       deterministic offline fixture (tagged, never valid for metrics)
  guardrails.py       deterministic RBI rule functions
  executor.py         Razorpay Payment Links + kill switch + backoff
  webhook_handler.py  HMAC-SHA256 verification, flips outcome → recovered
  audit_log.py        append-only JSONL, idempotent updates
  pipeline.py         wires the core loop
  report.py           metrics, per-cause and hard-case breakdowns
data/generate_dataset.py    reproducible synthetic batch, seeded
scripts/cancel_test_links.py  clears test-mode links between demo runs
run_batch.py          demo entry point
app.py                FastAPI: webhook receiver + demo endpoints
```

Synthetic input data is generated, never hand-typed, so the distribution is reproducible and provably not cherry-picked:

```bash
python data/generate_dataset.py --count 60 --seed 42
```

---

## Testing

```bash
python -m pytest        # 90 tests
```

Covers the guardrail rules including the revoked-mandate override, exposure caps enforced across a batch, webhook signature rejection and idempotent duplicate delivery, rate-limit backoff, provider routing, and a guard proving the suite never reaches a live API.

---

## Deployment

`render.yaml` provisions the service as a Render blueprint: connect the repo, supply four secrets (`GROQ_API_KEY`, `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET`, `RAZORPAY_WEBHOOK_SECRET`), deploy. Secrets are marked `sync: false` so they never enter git, and a pre-commit hook blocks any commit containing a key.

Deployment is a correctness requirement here, not a convenience: a signature-verified webhook needs an endpoint Razorpay can actually reach. Point the Razorpay webhook at `https://<service>/webhook` with `payment_link.paid` enabled.

| Endpoint | Purpose |
|---|---|
| `/` | Audit console |
| `/api/dashboard` | Metrics and decision rows as JSON |
| `/webhook` | Razorpay receiver, HMAC-SHA256 verified |
| `/demo/run-batch?limit=N` | Run a live batch |
| `/healthz` | Health check, reports active provider and kill-switch state |

## Production readiness

| Layer | Status |
|---|---|
| Core recovery loop | Built |
| Compliance guardrails | Built — deterministic, tested |
| Audit log | Built — immutable, idempotent, exportable |
| Human escalation routing | Built — confidence threshold |
| Kill switch | Built |
| Audit console | Built — deployed |
| Webhook-verified recovery | Built — confirmed against live Razorpay |
| Full RBI rule coverage beyond demo cases | Designed |
| Reviewer workflow UI, drift detection | Designed |

---

## Limitations

Stated plainly, because a reviewer will find them anyway:

- **`confidence` is model self-report, not calibrated.** Measured across two runs it did not reliably separate correct answers from incorrect ones. It is used only as a one-way escalation trigger.
- **Input data is synthetic.** Real API execution is on the output/recovery side, which is where correctness actually matters. Failure codes are real; the failures themselves are generated.
- **Accuracy is small-sample and provider-dependent.** 60 cases on one model. Treat it as directional.
- **The mandate-state guardrail did not fire in the measured run** — the model never recommended retrying a revoked mandate, so the rule had nothing to catch. It is covered by explicit tests rather than left to a dataset coincidence.
- **Test-mode Razorpay only.** No real money moves at any point.
- **One verified recovery, not a recovery rate.** ₹9,999 was confirmed end to end by a real webhook. That proves the mechanism, not a conversion percentage — claiming a recovery rate would need real customers, not synthetic ones.

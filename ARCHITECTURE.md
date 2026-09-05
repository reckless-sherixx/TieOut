# Tieout — Architecture

Reconciliation for merchants who get **one bank credit for many orders**, net of
deductions that do not line up.

This document explains how the app works. It is deliberately short: the detailed
reasoning behind each decision lives in the module docstrings, next to the code
it constrains, where it cannot drift into a separate file. Where a section here
is a summary, it names the module to read.

---

## 1. The problem shape

63 customers pay ₹49,320 on Tuesday. On Thursday the bank shows **one** credit:

```
63 orders                          ₹49,320.00
  − MDR (2.36%)                    ₹ 1,163.95
  − GST on that MDR (18%)          ₹   209.51
  − a refund from LAST week        ₹   890.00
  − a chargeback being held        ₹   500.00
──────────────────────────────────────────────
one line in the bank statement     ₹46,556.54
```

Three sources, none wrong, none agreeing. This is **many-to-one with
deductions**, and one deduction belongs to a different settlement period — so it
is not a `VLOOKUP` and not a join. The engine has to *reconstruct* a credit from
a set of legs before it can claim a match.

---

## 2. The pipeline

Two doors, one pipeline. A merchant's own export enters at stage 0 and becomes
the same typed records the seeded generator produces. **From stage 1 down,
nothing can tell which door it came through.**

```
  a merchant's export                     the seeded generator
      |                                        |
      v                                        |
 ┌─────────────────────────────────────┐       |
 │ 0  ADAPT         core/adapters/     │       |  no adapter needed:
 │                  api/ingest.py      │       |  already canonical
 │  detect by header shape -> parse    │       |
 │  MAY: quarantine an unreadable row  │       |
 │  MAY NOT: guess a value, drop a row │       |
 │       silently, keep a refused file │       |
 └─────────────────────────────────────┘       |
      |                                        |
      +--------------------+-------------------+
                           v
              orders.csv  psp.csv  bank.csv
                           |
 ┌─────────────────────────────────────┐
 │ 1  INGEST        core/ingest/       │  CSV -> typed pydantic records.
 │                                     │  MAY NOT: arithmetic, matching.
 └─────────────────────────────────────┘
                           |
 ┌─────────────────────────────────────┐
 │ 2  CANONICALIZE  core/canonicalize/ │  narration -> settlement_id, utr
 │                  core/matcher/pool  │  duplicate suppression, order recovery
 │                                     │  MAY NOT: decide a match.
 └─────────────────────────────────────┘
                           |
 ┌─────────────────────────────────────┐
 │ 3  MATCH         core/matcher/      │  T0 -> T1 -> T2 -> T3, in order.
 │                    tiers.py         │  MAY NOT: guess under ambiguity;
 │                    engine.py        │  MAY NOT: read truth.json.
 └─────────────────────────────────────┘
                           |
      +--> matches (MatchGroup)
                           |
 ┌─────────────────────────────────────┐
 │ 4  RESIDUE       core/matcher/      │  every unmatched subject, typed, with a
 │                    engine.py        │  machine-readable reason code.
 │                                     │  Invariant: every subject is matched or
 │                                     │  excepted, exactly once.
 └─────────────────────────────────────┘
                           |
                           v   (only when use_llm and a key are present)
 ┌─────────────────────────────────────┐
 │ 5  LLM ANALYST   core/llm/          │  proposes Hypothesis objects.
 │                    analyst.py       │  MAY NOT: compute money; see the whole
 │                                     │  batch; see truth.json; accept anything.
 └─────────────────────────────────────┘
                           |
 ┌─────────────────────────────────────┐
 │ 6  VERIFIER      core/llm/          │  six checks, all must hold.
 │                    verifier.py      │  MAY: reject. That is all it does.
 │                                     │  MAY NOT: read self_confidence.
 └─────────────────────────────────────┘
                           |
 ┌─────────────────────────────────────┐
 │ 7  REPORT        scorer/            │  metrics vs truth.json
 │                  core/store/        │  SQLite; audit trail
 │                                     │  Only module that grades. Never
 │                                     │  imported by the matcher.
 └─────────────────────────────────────┘
```

**Stage 6 is the load-bearing idea:** the LLM proposes and deterministic code
disposes. The headline number stays checkable even though a non-deterministic
component participated, because nothing non-deterministic can put a number into
the output.

---

## 3. The tier ladder

Tried in order, easiest first (`core/matcher/tiers.py`):

| | matches on | confidence |
|---|---|---|
| **T0** | a settlement reference in the narration **and** exact arithmetic | 1.00 |
| **T1** | one payment leg, exact amount, ±2-day window, exactly one candidate | 0.95 |
| **T2** | many legs netting to one credit — **the real problem shape** | 0.90 |
| **T3** | ±100 paise, cardinality-agnostic — rounding | 0.80 |
| **LLM** | an obfuscated reference, verified deterministically | 0.70 |

T0 requires the arithmetic *as well as* the reference. A narration can name a
settlement that does not reconstruct to the credit, and a reference alone would
make that a confident false match.

### The ambiguity rule

**More than one candidate ⇒ match nothing.**

Candidacy is **cardinality-blind**: the pool is not partitioned by payment-leg
count before the rule is applied. Partitioning first would make the T1/T2 label
act as a tie-breaker, turning two genuinely ambiguous settlements into two
confident false matches — one in each bucket. This single decision is why
`false_match_rate` is 0.000 and why `auto_match_rate` is not higher.

---

## 4. The analyst, and why its confidence is ignored

Some references are readable by a human and not by a regex —
`NEFT/RZP/SETL-00046/AUG26` where the code expects `setl_00046`. The analyst
reads the residue and proposes.

Every proposal then passes six deterministic checks in `core/llm/verifier.py` —
`existence`, `exclusivity`, `causality`, `arithmetic`, `coherence`,
`uniqueness` — plus the pipeline's own `subject_tie`. **Fail one, rejected.**

The model returns a `self_confidence`. It is stored, displayed, and **never an
input to acceptance**. The confidence attached to an accepted match is the 0.70
the *engine* stamps on the tier, not the number the model reported about itself.

---

## 5. Money

**`int` paise, never float.** Percentages are `(amount * bps) // 10_000`.

A float rupee amount cannot represent 0.1 exactly; summing 63 of them drifts,
and an engine that reports a residual delta of 0.000001 has invented a
discrepancy. `core/money.py` is the only place a currency value is formatted.

---

## 6. The separation that makes the numbers mean anything

Three modules, one direction of dependency:

```
core/generator/   emits the data AND truth.json
core/matcher/     may import NEITHER generator NOR scorer
scorer/           reads truth.json, grades the result
```

`tests/test_boundaries.py` walks the import graph and proves the perimeter holds
— and documents the two ways it could still be bypassed. Without this, "87.6%
matched" is a number the thing being measured could have influenced.

---

## 7. Where files come from

Three doors into the same engine; none gets a shortcut past sniff-and-quarantine.

**Upload** (`api/ingest.py`). Content SHA-256 is the identity, so re-uploading
the same bytes parses nothing and writes nothing. Seven adapters sniff the file
and score it; below threshold it is refused **with every candidate score**, not
a bare "unrecognised". Unreadable rows are quarantined with their raw text — the
file still ingests. Blobs are stored AES-256-GCM with the address as AAD.

One rule is enforced at this boundary for every door
(`api/ingest.py::enforce_visible_outcome`): **an adapter that accepted a file
and produced no records must say why.** `records == [] and quarantined == []` is
never a valid outcome. It lives here rather than in the adapters so the eighth
adapter inherits it instead of having to remember it.

**The mailbox** (`core/connectors/imap_mailbox.py`, `api/connections.py`). There
is no pull API for an Indian bank statement outside the RBI Account Aggregator
network, and FIU registration is restricted to entities regulated by
RBI/SEBI/IRDAI/PFRDA — Razorpay qualifies, a standalone tool does not. So the
system reads the mail the bank already sends.

A credential is entered once and stored **AES-256-GCM with
`connection_id:field_name` bound in as additional authenticated data**, so a
database write cannot move one merchant's ciphertext into another's row. There
is no plaintext mode: a missing key makes the endpoint refuse.
`ConnectionSummary` carries `has_password: bool` and **no password field by
construction**, so no endpoint can return it. The mailbox is opened read-only —
`readonly=True` *and* `BODY.PEEK[]`, two independent guards — and the search is
scoped to named senders and a date window. Credit reports are refused **by name,
before they are read**, and every skipped attachment is listed: one silently
skipped is indistinguishable from one the bank never sent.

**The scheduler** (`api/scheduler.py`). Wakes daily, fetches any mailbox whose
last success is 30 days old. A failed sync does not advance the clock, so the
next tick retries rather than skipping a month.

---

## 8. Two doors, two honesty modes

`api/jobs.py` has `execute_run` (seeded) and `execute_run_over_uploads`
(merchant files). **Below those two functions the code is one function**, so a
divergence between them is not something the matcher, analyst or scorer could
express even by accident. `tests/round_trip/test_upload_path.py` holds both to
identical results on identical data.

The difference is what an upload set does not have:

- **`truth.json`** — and there cannot be one; nobody knows the right answer to a
  merchant's own books. So `metrics` stays **null** and the run reports what it
  found rather than how well it did. The console renders that absence as one.
- **`psp_gst_invoice.csv`** — so no ITC reconciliation runs.

---

## 9. Results

Deterministic path only, no LLM. Seeded and byte-reproducible:

```
python -m uv run recon generate --seed 42 --count 5000
python -m uv run recon run --dataset fixtures/seed42-5000 --no-llm
```

| Dataset | auto_match_rate | recall_on_resolvable | **false_match_rate** | **trap_capture_rate** | records/sec |
|---|---|---|---|---|---|
| `fixtures/tiny` | 1.000 | 1.000 | **0.000** | **1.000** | — |
| `fixtures/seed42-50` | 0.800 | 0.800 | **0.000** | **1.000** | 31,706 |
| `fixtures/seed42-500` | 0.876 | 0.876 | **0.000** | **1.000** | 37,160 |
| `fixtures/seed42-5000` | 0.873 | 0.873 | **0.000** | **1.000** | 29,730 |

`records/sec` is the only figure here that does not reproduce on another
machine. 50,000 records cost 1.99 s where the pre-index engine needed 141 s,
with **every metric held byte-identical across that change** by
`tests/test_scale_acceptance.py`.

`trap_capture_rate` is the one worth arguing about. The generator plants pairs
that are **impossible** to resolve from the data given. An engine that guesses
scores 0 here while its match rate goes *up*.

---

## 10. What it cannot do

- **Split settlements are the entire accuracy gap.** One settlement paid across
  two bank lines satisfies `Σ legs == credit₁ + credit₂` and matches neither
  line alone. Resolving it means searching subsets of bank lines — which is
  where a tie-breaker on an ambiguous set creeps back in, and at 5,000 records
  there will be spurious pairs that sum to some unrelated settlement's net.
  Trading a `false_match_rate` of exactly 0.000 for a bigger match rate is the
  wrong trade in accounting. It is reported as a typed exception instead.
- **Five of seven adapters have never seen a real file.** Their layouts come
  from published schemas. `slice-pdf-v1` is verified against a genuine bank
  artefact; the Razorpay settlement layout stays UNVERIFIED because test mode
  blocks settlement creation.
- **`recon run --llm` does not run the analyst.** One wiring point only:
  `api/jobs.py::execute_run`, via `POST /api/runs` with `use_llm: true` and a
  provider key set. One accept loop, one caller — and it keeps `core/matcher/`
  free of anything that could reach `truth.json`.
- **The SQLite database is not encrypted.** Blobs and credentials are; the
  database is the largest gap in the posture, and the honest fix is a managed
  database, not a library.
- **No retention or erasure job.** Nothing is deployed.

---

## 11. Running it

```
docker compose up --build
```

Console on http://localhost:3000, API on http://localhost:8000/docs.

**Two images, not one.** A Python ASGI app and a Node server are two runtimes
and two failure modes; behind a supervisor in one image a crash in either is
invisible from outside and `docker compose logs web` does not exist.

Three things in that setup are load-bearing and none is obvious:

**`NEXT_PUBLIC_*` is inlined at build time**, so it lives in `build.args`, never
`environment:`. `NEXT_PUBLIC_API_MOCKING` defaults to `enabled`, so an image
built without `disabled` serves a bundle answering from MSW mocks while the API
container sits beside it, healthy and unused. `web/Dockerfile` greps its own
build output and **fails the build** rather than let that ship.

**`NEXT_PUBLIC_API_BASE` is the URL the *browser* uses.** The browser runs on
the host, outside the compose network, so `http://api:8000` — what the service
name suggests — resolves only between containers and fails in the tab.

**`web/.dockerignore` is not optional**, and the root one does not cover it:
compose builds with `context: ./web`, and Docker resolves `.dockerignore`
relative to the context. Without it the context is `node_modules` plus `.next` —
measured at 300 MB.

### On the host

`uv` is installed as a Python package and its shim is not on PATH, so invoke it
as `python -m uv`.

```
python -m uv run pytest                                      # 1623 passed
python -m uv run recon run --dataset fixtures/seed42-500 --no-llm
python -m uv run uvicorn api.main:app --reload --port 8000
cd web && npm run dev                                        # localhost:3000
```

---

## 12. Where the rest of the detail lives

This file is a map. The reasoning behind each rule is a docstring on the module
that enforces it, so it cannot drift from the code:

| Question | Read |
|---|---|
| Why candidacy is cardinality-blind | `core/matcher/tiers.py` |
| What each of the six checks proves | `core/llm/verifier.py` |
| Why the credential AAD is `id:field` | `core/store/secretbox.py` |
| Why a silent parse is refused | `api/ingest.py::enforce_visible_outcome` |
| Why the mailbox has two read-only guards | `core/connectors/imap_mailbox.py` |
| Why the scorer cannot be gamed | `tests/test_boundaries.py` |
| Why a metric has the denominator it has | `scorer/metrics.py` |

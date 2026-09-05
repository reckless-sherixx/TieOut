# Tieout

**Reconciliation for merchants who get one bank credit for many orders.**

Sales register + PSP settlement report + bank statement in; a match rate you can
check against ground truth, and an itemised exception list where every exception
has a named cause, out.

Built for the Razorpay AI Buildathon, Track 04 — AI Finance Controller.

---

## The problem

On Tuesday, 63 customers pay a merchant ₹49,320. On Thursday the bank shows
**one** credit of ₹46,556.54.

```
63 orders                          ₹49,320.00
  − MDR (2.36%)                    ₹ 1,163.95
  − GST on that MDR (18%)          ₹   209.51
  − a refund from LAST week        ₹   890.00
  − a chargeback being held        ₹   500.00
──────────────────────────────────────────────
one line in the bank statement     ₹46,556.54
```

Three sources, none of them wrong, none of them agreeing. This is not a
`VLOOKUP`: the shape is **many-to-one with deductions**, and one of the
deductions belongs to a different settlement period. Finance teams do it by hand,
in Excel, for days each month.

## What it does

Two doors into one engine.

**The seeded door** generates a labelled adversarial dataset with 13 defect
classes and grades the run against its own `truth.json`. This is what makes every
accuracy number here checkable rather than assertable — including
`false_match_rate`, which is the number a reconciliation vendor does not show you.

**The merchant's door** takes files somebody actually exported — a Razorpay
settlement report, an HDFC or ICICI statement, an MT940, a Shopify order export,
a Delhivery COD remittance, a Slice PDF statement — sniffs the format,
quarantines the rows it cannot read, and runs the same matcher over them. A run
through this door reports what it matched and excepted and **no rate at all**,
because nothing graded it. That absence is a feature, and the console renders it
as one.

## Quickstart — Docker

The only prerequisite is Docker Desktop (or Docker Engine with the Compose
plugin). Nothing else needs installing: no Python, no Node, no database.

**1. Start both services with one command**

```bash
docker compose up --build
```

First build takes a few minutes. Add `-d` to run it in the background.

**2. Open the console**

| | |
|---|---|
| Console | http://localhost:3000 |
| API docs | http://localhost:8000/docs |

Compose waits for the API to report healthy before starting the console, so if
the page loads, the backend is already up.

**3. Make a run**

Click **New run** on the console, keep the defaults (seed 42, 500 records) and
submit. That generates a labelled adversarial dataset and reconciles it — you
should see **87.6% auto-matched, 0.0% false matches, 10 of 10 traps declined**.
Those figures are seeded, so they reproduce exactly on your machine.

To do the same from the command line:

```bash
curl -s -X POST http://localhost:8000/api/datasets/generate -H 'Content-Type: application/json' -d '{"seed":42,"record_count":500}'
```

```bash
curl -s -X POST http://localhost:8000/api/runs -H 'Content-Type: application/json' -d '{"dataset_id":"PASTE_ID_FROM_ABOVE","use_llm":false}'
```

**4. Stop it**

```bash
docker compose down
```

The database lives in a named volume and survives a restart. Add `-v` to
discard it.

### Optional: a `.env`

Picked up automatically if present. It is what enables the Gemini analyst and
the mailbox connector — and it puts real credentials inside a container, so it
is the right default on your own machine and the wrong one anywhere shared.

Without it the app still runs: the analyst is simply unavailable, and
`POST /api/connections` answers 422 naming `RECON_BLOB_KEY` rather than storing
a credential it cannot encrypt.

### If something goes wrong

| Symptom | Cause |
|---|---|
| `port is already allocated` | Something else holds 3000 or 8000. Stop it, or change the published port in `compose.yaml`. |
| Console shows data but the API log is silent | The console is serving MSW mocks. `NEXT_PUBLIC_*` is inlined at **build** time, so rebuild with `docker compose up --build` rather than restarting. |
| `docker compose ps` shows `web` waiting | Normal — it starts only once `api` reports healthy. |

### Or without Docker

`uv` is installed as a Python package and its shim is not on PATH, so invoke it
as `python -m uv`.

```bash
python -m uv sync
```

```bash
python -m uv run pytest
```

```bash
python -m uv run recon run --dataset fixtures/seed42-500 --no-llm
```

API and console:

```bash
python -m uv run uvicorn api.main:app --reload --port 8000
```

```bash
cd web && npm install && npm run dev
```

The console's `NEXT_PUBLIC_*` variables are inlined at **build** time. A build
made without them serves a bundle that answers from MSW mocks while the API sits
idle — the one thing that bites when demoing.

## Results

Deterministic path only, no LLM. The generator is seeded and byte-reproducible,
so any row reproduces from scratch:

```bash
python -m uv run recon generate --seed 42 --count 5000
```

| Dataset | auto_match_rate | recall_on_resolvable | **false_match_rate** | **trap_capture_rate** | records/sec |
|---|---|---|---|---|---|
| `fixtures/tiny` | 1.000 | 1.000 | **0.000** | **1.000** | — |
| `fixtures/seed42-50` | 0.800 | 0.800 | **0.000** | **1.000** | 31,706 |
| `fixtures/seed42-500` | 0.876 | 0.876 | **0.000** | **1.000** | 37,160 |
| `fixtures/seed42-5000` | 0.873 | 0.873 | **0.000** | **1.000** | 29,730 |

`records/sec` is the only figure here that does not reproduce on another machine.
50,000 records cost 1.99 s, where the pre-index engine needed 141 s, with every
metric held byte-identical across the change by `tests/test_scale_acceptance.py`.

`trap_capture_rate` is the one worth arguing about. The generator plants pairs
that are **impossible** to resolve from the data given. An engine that guesses
scores 0 here while its match rate goes up.

## How it works

**A tiered ladder, easiest first.**

| | matches on |
|---|---|
| **T0** | a settlement reference in the bank line **and** exact arithmetic |
| **T1** | one payment leg, exact amount, ±2-day window, exactly one candidate |
| **T2** | many payment legs netting to one credit — the real problem shape |
| **T3** | ±100 paise, cardinality-agnostic — rounding |

**The ambiguity rule.** More than one candidate ⇒ match nothing. Candidacy is
cardinality-blind: partitioning the pool by payment-leg count before the rule
applies would make the T1/T2 label a tie-breaker and turn two ambiguous
settlements into two confident false matches.

**The LLM proposes; deterministic code disposes.** Some references are readable
by a human and not by a regex — `NEFT/RZP/SETL-00046/AUG26` when the code expects
`setl_00046`. An analyst model reads the residue and proposes. Every proposal
then passes six deterministic checks — `existence`, `exclusivity`, `causality`,
`arithmetic`, `coherence`, `uniqueness` — plus the pipeline's `subject_tie`. Fail
one, rejected. The model's own confidence is **never** an input to acceptance.

**Money is `int` paise, never float.** Percentages are `(amount * bps) // 10_000`.

**The scorer cannot be gamed by the matcher.** `core/generator/` emits the data
and the truth, `core/matcher/` must never read the truth, and `scorer/` compares.
`tests/test_boundaries.py` walks the import graph and proves the perimeter holds
— and documents the two ways it can still be bypassed.

How the whole pipeline fits together, stage by stage, is in
**[ARCHITECTURE.md](ARCHITECTURE.md)**.

## Where the files come from

Three doors into the same engine, and none of them gets a shortcut past the
sniff-and-quarantine path.

**Upload.** Drag a file onto `/uploads`.

**The mailbox.** There is no pull API for an Indian bank statement outside the
RBI Account Aggregator network, and FIU registration there is open only to
entities regulated by RBI, SEBI, IRDAI or PFRDA — Razorpay qualifies; a
standalone tool does not. The bank already emails the statement every month, so
`/connections` reads that mail. A credential is entered once, stored
**AES-256-GCM with the connection id bound in as additional authenticated
data**, and returned by no endpoint. The mailbox is opened read-only —
`BODY.PEEK`, no flag set, nothing moved — and the search is scoped to named
senders and a date window rather than the whole mailbox. Credit reports are
refused **by name, before they are read**, and every skipped attachment is
listed in the sync result: one silently not fetched would be indistinguishable
from one the bank never sent, and only one of those means the control works.

**The scheduler.** A loop wakes daily and fetches any mailbox whose last
success is 30 days old. A failed sync does not advance the clock, so the next
tick retries rather than skipping a month.

## The report

```
GET /api/runs/{id}/report.pdf
```

The console shows the answer; the report defends it. Every derivation, every
denominator, what each rung requires and the confidence the engine stamped on
it, every exception with its cause, and what the run cannot tell you. It is a
document a finance team can file with a month-end close, which six browser tabs
are not — and its figures are tested equal to the run's wire values, because a
report that drifts from the run it describes is the same defect as a console
that hardcodes a confidence.

## What it cannot do

- **Split settlements and obfuscated references are the entire accuracy gap.**
  Every unmatched *resolvable* subject at 50, 500 and 5,000 records belongs to
  one of exactly those two classes — verified as set equality with no remainder,
  not as a sample.
- **Five of the seven adapters have never seen a real file.** Their layouts come
  from published schemas. `slice-pdf-v1` is the exception, verified against a
  genuine bank artefact; the Razorpay settlement layout stays UNVERIFIED because
  test mode blocks settlement creation, so the export could never be produced.
- **`recon run --llm` does not run the analyst.** The analyst has exactly one
  wiring point — `api/jobs.py::execute_run`, via `POST /api/runs` with
  `use_llm: true`. One accept loop, one caller, and it keeps `core/matcher/` free
  of anything that could reach `truth.json`.
- **The SQLite database is not encrypted.** Uploaded blobs are (AES-256-GCM, the
  blob's address bound as AAD); the database is the largest gap in the posture
  and the honest fix is a managed database, not a library.
- **No retention or erasure job.** Nothing is deployed.

## Layout

```
core/
  generator/     seeded adversarial data + truth.json
  matcher/       the tier ladder (may not import generator or scorer)
  llm/           analyst client + the six-check verifier
  adapters/      7 real-format readers, quarantine-never-crash
  ingest/        strict CSV ingest
  canonicalize/  transaction-type normalisation
  itc/           GST-on-MDR input tax credit reconciliation
  drift/         run-over-run comparison
  connectors/    pull a file instead of waiting for an upload
  store/         SQLite, encrypted blob store, encrypted credential vault
scorer/          grades a result against truth.json
report/          one run as a PDF that survives leaving the app
api/             FastAPI, 23 paths / 26 operations, openapi.yaml is the contract
web/             Next.js App Router console
tests/           the credibility layer
bench/           scale baselines held byte-identical across optimisation
fixtures/        tiny, seed42-50, seed42-500 committed; seed42-5000 regenerated
Dockerfile       the API image
web/Dockerfile   the console image, standalone Next output
compose.yaml     both of them, one command
```

## Stack

Python 3.12+ · pydantic v2 · typer · FastAPI · SQLModel/SQLite · pytest +
hypothesis · anthropic + google-genai · Next.js App Router · TypeScript ·
Tailwind · shadcn/ui · MSW

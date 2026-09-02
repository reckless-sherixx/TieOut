# Tieout — Architecture

A multi-source reconciliation engine: sales register + PSP settlement report +
bank statement, in, and a ground-truth-scored match rate with an itemised
exception list, out.

Everything below is checkable against the source. File paths and line numbers
are given so you can check it; line numbers are as of the commit that carries
this file.

---

## 1. The claim this document exists to defend

> **The deterministic engine does all money arithmetic. The LLM never touches
> arithmetic. Every LLM proposal is re-checked by deterministic code before
> acceptance.**

You can verify that yourself in about thirty seconds. There is exactly one
function in the repository that turns a set of PSP legs into money —
`reconstruct` in **`core/matcher/batch.py:45`** — and every caller of it is
visible in one grep:

```
$ grep -rn "reconstruct(" core/ scorer/ api/ --include=*.py | grep -v "def reconstruct"
core/llm/pipeline.py:374:    totals = reconstruct(legs)
core/llm/prompts.py:182:        totals = reconstruct(legs)
core/llm/verifier.py:166:    net = reconstruct([ctx.txns_by_id[i] for i in h.proposed_psp_txn_ids]).net
core/llm/verifier.py:193:        if abs(reconstruct(legs).net - credit) <= TOLERANCE_PAISE:
core/matcher/pool.py:173:            sid: reconstruct(legs) for sid, legs in self._legs.items()
core/store/repo.py:859:    totals = reconstruct(legs)
```

Six call sites. `pool.py` is the deterministic matcher and `repo.py` is the
store. The other four are the LLM layer, and all four call the matcher's own
function:

- **`core/llm/prompts.py:182`** — every money figure in the prompt (each
  candidate settlement's net and its component breakdown) is computed here,
  before the model sees it. The model is handed arithmetic; it is never asked
  to do any.
- **`core/llm/verifier.py:166`** (`_arithmetic`) — the re-check. The proposal's
  net is recomputed from the legs and compared against the bank credit.
- **`core/llm/verifier.py:193`** (`_uniqueness`) — enumerates which settlements
  close the line, using the same function.
- **`core/llm/pipeline.py:374`** (`_match_from`) — when a hypothesis is
  accepted, every money field on the resulting `MatchGroup` is recomputed from
  the legs. Nothing the model said is copied into a number.

`core/store/repo.py:859` is the settlements listing
(`GET /api/runs/{id}/settlements`), and it is a call site for the same reason
the LLM layer is one: a settlement that **never matched** has no `MatchGroup` to
read a breakdown off, so the API has to produce one — and the only way to do
that without becoming a second source of money is to call the matcher's own
function over the settlement's own legs. A settlement that *did* match is not in
this branch at all: its row copies the `MatchGroup` field for field.

Then open **`core/llm/verifier.py:267`** and read the six-entry `CHECKS` tuple,
and **`core/llm/verifier.py:277`** — `verify()` — which is nine lines: run every
check in order, return on the first failure. There is one accept loop in the
repository (`core/llm/pipeline.py:99`), and it calls `verify` before it can
build anything.

The third leg of the claim is a negative: `Hypothesis.self_confidence` is never
read by anything that decides. `grep -n self_confidence core/llm/*.py` shows it
only in the prompt schema, in the audit trail, and in the evidence string on an
accepted match. `tests/llm/test_verifier.py:607`
(`test_high_self_confidence_does_not_bypass_any_check`) pins it: a hypothesis
with `self_confidence=1.0` is rejected exactly as hard as one with 0.1. An
accepted LLM match carries `LLM_CONFIDENCE = 0.70` (`core/llm/pipeline.py:76`),
a constant below T3's 0.80, which is the loosest deterministic tier.

---

## 2. The problem shape — why this is not a VLOOKUP

The naive framing is "match invoice A to payment B". That is a VLOOKUP, and it
is not the problem. The real shape is **many-to-one with deductions**: N orders
collapse into one bank credit, net of MDR, net of GST on the MDR, net of refunds
that belong to a previous cycle, net of chargeback holds. The bank line does not
carry any of the components — only the residue.

### A worked example, with numbers you can check

From `fixtures/tiny/`. Settlement `setl_B2` → bank line `BL-0002`. It carries
all four deduction classes at once.

`fixtures/tiny/psp.csv`, the six legs of `setl_B2`:

```
txn_id      type         order_id      captured_at            amount    settled_at
pay_1101    payment      ORD-004510    2026-07-01T11:18:07   +2100000   2026-07-03
pay_1102    payment      ORD-004511    2026-07-02T09:33:29    +675000   2026-07-03
rfnd_2001   refund       ORD-004472    2026-06-29T14:20:11    -890000   2026-07-03
cb_7701     chargeback   ORD-004018    2026-06-30T08:05:00     -50000   2026-07-03
fee_1002    fee          (none)        2026-07-03T00:00:00     -65490   2026-07-03
tax_1002    tax          (none)        2026-07-03T00:00:00     -11788   2026-07-03
```

`fixtures/tiny/bank.csv`:

```
BL-0002,2026-07-03,NEFT CR RAZORPAY SOFTWARE PVT LTD SETL setl_B2,1757722,,16552376,HDFCN26070300142
```

The reconciliation:

```
  payment legs        ORD-004510        +2,100,000 paise      ₹21,000.00
                      ORD-004511          +675,000            ₹ 6,750.00
                                         ──────────
  payment gross                          2,775,000            ₹27,750.00

- refund              rfnd_2001            890,000            ₹ 8,900.00   ← cycle N-1
- chargeback hold     cb_7701               50,000            ₹   500.00   ← order not in register
- MDR      2.36% of   2,775,000             65,490            ₹   654.90
- GST      18%   of      65,490             11,788            ₹   117.88
                                         ──────────
  net                                    1,757,722            ₹17,577.22
  bank credit BL-0002                    1,757,722            ₹17,577.22   ← exact
```

Four things in that example are the whole difficulty:

1. **The bank line is net of everything.** No tier below T0 can match on a raw
   amount; every one of them has to rebuild the settlement from its legs first.
   That is why `core/matcher/batch.py` exists as a separate module and why the
   verifier imports it rather than re-implementing it.

2. **The refund is from a different cycle.** `rfnd_2001` refunds `ORD-004472`,
   which was captured on 2026-06-01 and settled in `setl_A1` on 2026-06-03. It
   is netted into the July batch. A matcher that assumes a settlement's orders
   are the orders captured in its own period gets this wrong. Note also that
   `ORD-004472` appears in *two* linkages in `truth.json`, once as a payment in
   `setl_A1` and once as a refund in `setl_B2`.

3. **The MDR base is the settlement's own payment legs, and nothing else.**
   2,775,000 × 236 bps = 65,490 exactly. If you had derived the fee base from
   the settlement *net*, or from gross-minus-refunds (2,775,000 − 890,000 −
   50,000 = 1,835,000), you would get 43,306 and the batch would not close. Real
   MDR is not returned when a payment is refunded. This is why
   `core/matcher/batch.py:73` has a separate `payment_gross` function with a
   docstring explaining that `reconstruct`'s `gross` is a *reporting* figure and
   not the fee base.

4. **The chargeback names an order that does not exist.** `cb_7701` references
   `ORD-004018`, which is not in `orders.csv`. It is dropped from the
   settlement's order set (`truth.json` records `["ORD-004510", "ORD-004511",
   "ORD-004472"]` — three orders for six legs) and the fact is written to the
   audit trail. It is *not* raised as an exception: the arithmetic still closes
   and the bank line still matches. See `core/matcher/pool.py`, the
   `dangling_order_refs` view.

---

## 3. The pipeline

Two doors, one pipeline. A merchant's own export enters through stage 0 and is
turned into the same typed records the canonical CSVs produce; from stage 1
down, nothing can tell which door it came through. §10 and §11 are that door in
full.

```
  a merchant's export                     the seeded generator
      |                                        |
      v                                        |
 ┌─────────────────────────────────────┐       |
 │ 0  ADAPT         core/adapters/     │       |  no adapter needed:
 │                  api/ingest.py      │       |  already canonical
 │                                     │       |
 │  detect by header shape -> parse    │       |
 │  MAY: quarantine a row it cannot    │       |
 │       read, with its raw text.      │       |
 │  MAY NOT: guess a value, drop a     │       |
 │       row silently, or retain a     │       |
 │       file it refused.              │       |
 └─────────────────────────────────────┘       |
      |                                        |
      +--------------------+-------------------+
                           |
                           v
  orders.csv     psp.csv     bank.csv
      |             |            |
      v             v            v
 ┌─────────────────────────────────────┐
 │ 1  INGEST        core/ingest/       │  CSV -> typed pydantic records.
 │                                     │  MAY: parse, coerce, reject a bad row.
 │                                     │  MAY NOT: arithmetic, matching.
 └─────────────────────────────────────┘
      |
      v
 ┌─────────────────────────────────────┐
 │ 2  CANONICALIZE  core/canonicalize/ │  narration -> (settlement_id, utr, entity)
 │                  core/matcher/pool  │  duplicate suppression, order recovery
 │                                     │  MAY: derive views, record findings.
 │                                     │  MAY NOT: decide a match.
 └─────────────────────────────────────┘
      |
      v
 ┌─────────────────────────────────────┐
 │ 3  MATCH         core/matcher/      │  T0 -> T1 -> T2 -> T3, in order.
 │                    tiers.py         │  MAY: claim a settlement for a line.
 │                    batch.py         │  MAY NOT: guess under ambiguity;
 │                    engine.py        │  MAY NOT: read truth.json.
 └─────────────────────────────────────┘
      |
      +--> matches (MatchGroup)
      |
      v
 ┌─────────────────────────────────────┐
 │ 4  RESIDUE       core/matcher/      │  every subject not matched, typed,
 │                    engine.py        │  with a machine-readable reason code.
 │                                     │  Partition invariant: every subject is
 │                                     │  matched or excepted, exactly once.
 └─────────────────────────────────────┘
      |
      v   (only when --llm and a key are present)
 ┌─────────────────────────────────────┐
 │ 5  LLM ANALYST   core/llm/          │  proposes Hypothesis objects.
 │                    prompts.py       │  MAY: read the residue + candidates.
 │                    analyst.py       │  MAY NOT: compute money; see the whole
 │                                     │  batch; see truth.json; accept anything.
 └─────────────────────────────────────┘
      |
      v
 ┌─────────────────────────────────────┐
 │ 6  VERIFIER      core/llm/          │  six checks, all must hold.
 │                    verifier.py      │  MAY: reject. That is all it does.
 │                    pipeline.py      │  MAY NOT: read self_confidence;
 │                                     │  MAY NOT: widen a deterministic rule.
 └─────────────────────────────────────┘
      |
      v
 ┌─────────────────────────────────────┐
 │ 7  REPORT        scorer/            │  metrics vs truth.json
 │                  core/store/        │  SQLite; audit trail
 │                                     │  MAY: read truth.json. Only module that
 │                                     │  grades. Never imported by the matcher.
 └─────────────────────────────────────┘
```

Stage 6 is the load-bearing idea: the LLM proposes and deterministic code
disposes. The headline number stays checkable even though a non-deterministic
component participated, because nothing non-deterministic can put a number into
the output.

---

## 4. Money

`core/money.py` is 28 lines: six of code, and a sixteen-line docstring on the
one function that could go wrong.

```python
Money = int   # ALWAYS paise. Never float. Never Decimal in transport.

def pct_of(amount: Money, bps: int) -> Money:
    return (amount * bps) // 10_000
```

**Why.** A float in money code is a credibility loss in a payments context, and
it is not a theoretical one: 2.36% of ₹49,320.00 in binary floating point is not
a whole number of paise, and the error compounds once per settlement. Everything
here is integer arithmetic. Percentages are basis points floored with `//`:
MDR is 236 bps, GST is 1800 bps.

`Decimal` is excluded from *transport* specifically. Serialising a `Decimal`
means choosing a string format, and every layer that parses it back chooses
again. An `int` of paise has exactly one representation on the wire, in JSON, in
SQLite, and in TypeScript.

The floor direction is documented and pinned rather than assumed. `//` floors
toward negative infinity, so `pct_of(-x, bps) != -pct_of(x, bps)` — the two
differ by one paise. Callers must not pass a negative base; every percentage
base in this project is a sum of `payment` legs and is non-negative, and sign is
carried by the PSP leg's own `amount`. `tests/test_money.py:14` and `:21` pin
the asymmetry so it cannot drift into a silent one-paise bug.

Worked against the fixture, from `tests/test_money.py:3`:

```python
pct_of(4_932_000, 236) == 116_395    # MDR on setl_A1's ₹49,320.00 gross
pct_of(  116_395, 1800) == 20_951    # GST on that MDR
```

Both match `fee_1001` and `tax_1001` in `fixtures/tiny/psp.csv` exactly.

One inconsistency, stated rather than hidden: `core/money.py::fmt_inr` uses
Western digit grouping (`₹696,193.01`) while `web/lib/money.ts::formatINR` uses
Indian lakh/crore grouping (`₹6,96,193.01`). The web formatter is the one a
finance reader sees and its docstring records the ruling; `fmt_inr` is a
CLI/debug helper and has not been brought into line. They disagree.

---

## 5. The tier ladder

Four deterministic tiers, run in order. A subject matched at an earlier tier is
removed from the pool before the next runs (`core/matcher/engine.py`, the loop
over `TIERS`).

| Tier | Rule | Cardinality | Tolerance | Date window | Confidence |
|---|---|---|---|---|---|
| **T0** | reference hit **and** exact arithmetic | any | 0 | none | 1.00 |
| **T1** | reconstruction closes exactly | exactly one payment leg | 0 | ±2 days | 0.95 |
| **T2** | reconstruction closes exactly | two or more payment legs | 0 | ±2 days | 0.99 |
| **T3** | reconstruction closes within tolerance | any | ±100 paise | ±2 days | 0.80 |

Source: `core/matcher/tiers.py` — `_T0` at :259, `_T1` at :369, `_T2` at :389,
`_T3` at :407. `WINDOW_DAYS = 2` at :53, `TOLERANCE_PAISE = 100` at :57.

### T0 requires the arithmetic as well as the reference

A settlement id in a narration proves **identity**. It says nothing about
whether the sum closes. `_T0._candidates` (`core/matcher/tiers.py:272`) finds
the reference, reconstructs the settlement, and *returns no candidate* if the
delta is non-zero — writing the reason into the audit trail and letting the line
fall through to T1/T2/T3.

This is not pedantry. `setl_D4` in `fixtures/tiny/` names itself in `BL-0004`'s
narration and reconstructs to 2,916,456 paise against a credit of 2,916,406 — a
50-paise break, the `rounding_break` defect. Without the arithmetic clause, T0
would claim that line at confidence 1.00 and write a `MatchGroup` whose `net` is
not the bank credit, breaking the invariant asserted in `core/models.py:80`. It
would also swallow the one defect whose entire purpose is to exercise the
T3-versus-exception boundary, so T3 would never run end to end. Instead T0
declines, T1 and T2 decline on tolerance 0, and T3 takes it at 0.80 with
`delta=50` recorded in evidence. Pinned by
`tests/matcher/test_tiers.py:34` and `:44`.

T0 deliberately does not apply the date window: an explicit reference plus an
exactly closing sum is not made more or less certain by a late posting
(`tests/matcher/test_tiers.py:245`).

### T1 and T2 split by payment-leg cardinality, not by method

Every tier below T0 reconstructs from legs, because the bank credit is net of
fees. **Method therefore cannot distinguish T1 from T2** — an earlier draft
defined T1 as "reconstruction + date window", which is T2's method exactly, so
T1 subsumed T2 and the tier the project calls its core would never have fired.

The distinction is cardinality:

- **T1** — the settlement has **exactly one** `payment` leg.
- **T2** — the settlement has **two or more** `payment` legs.

`fee`, `tax`, `refund`, `chargeback`, `reserve` and `adjustment` legs do **not**
count. A settlement with one payment, one fee and one tax leg is T1: it settles
one order, and the deduction legs are the arithmetic, not the batch. The count
lives in one place, `core/canonicalize/txn_types.py::PAYMENT_TYPE`, and is
computed by `CandidatePool.payment_legs` (`core/matcher/pool.py:441`) and
`batch.payment_leg_count` (`core/matcher/batch.py:96`).

T3 is cardinality-agnostic by design. Restricting it to T2's cardinality would
strand a single-payment-leg settlement carrying a rounding break — which is
exactly `setl_D4`'s shape. `tests/matcher/test_tiers.py:220`.

### Why candidacy must be cardinality-blind

**This is a real bug that was caught in review, and it is the most interesting
thing in the matcher.**

The tempting implementation is: T1 searches the pool of one-payment-leg
settlements, T2 searches the pool of many-payment-leg settlements. Every local
rule then reads as correct. T1's rule is right. T2's rule is right. The
ambiguity rule is right. The tests for each pass.

And `trap_capture_rate` silently goes to zero.

`fixtures/tiny/` contains the trap. `BL-0005` and `BL-0006` are identical:
credit 2,430,380 paise, date 2026-07-24, narration `NEFT CR   PAYOUT`, no UTR,
no settlement reference. Two settlements close them:

```
setl_K9   pay_1301 +1,600,000  (ORD-004720)      2 payment legs
          pay_1302   +900,000  (ORD-004721)
          fee_1005    -59,000
          tax_1005    -10,620
          ─────────────────────
          net       2,430,380

setl_M2   pay_1401 +2,500,000  (ORD-004735)      1 payment leg
          fee_1006    -59,000
          tax_1006    -10,620
          ─────────────────────
          net       2,430,380
```

Same net, same date. The data does not determine which settlement funded which
bank line, and `truth.json` records both lines in `unresolvable_ids`.

Now filter the candidate pool by cardinality before applying the ambiguity rule:

```
                  CORRECT                        BROKEN
                  (cardinality-blind)            (pool partitioned first)

  T1 sees     candidates {K9, M2}            candidates {M2}   <- K9 filtered out
              -> 2 candidates, ambiguous     -> 1 candidate, "unique"
              -> match nothing               -> MATCH M2   (a guess)

  T2 sees     candidates {K9, M2}            candidates {K9}   <- M2 filtered out
              -> 2 candidates, ambiguous     -> 1 candidate, "unique"
              -> match nothing               -> MATCH K9   (a guess)
```

Nothing in the code looks wrong. The cardinality filter has become a
**tie-breaker on an ambiguous set**, which is the one thing the design forbids
outright — and it did so implicitly, as a side effect of an optimisation, in a
place where no rule says "tie-break".

That one settlement happens to batch two orders and the other does not is not a
distinguishing signal. It is an accident of shape.

#### What the failure actually costs — measured, not assumed

This is worth being exact about, because the obvious statement of the blast
radius is wrong and the difference is the whole reason the test is shaped the
way it is.

Patching `_ReconstructionTier._candidates` to filter by cardinality and re-running
`fixtures/tiny/`:

| Subjects in the pool | Correct build | Broken build |
|---|---|---|
| **One** trap line (`BL-0005` alone) | unmatched | **matched, T1 → `setl_M2`** |
| **Both** trap lines (the fixture as shipped) | both unmatched, `trap_capture_rate` 1.0 | both unmatched, `trap_capture_rate` **1.0** |

With **both** trap lines present, the broken build still scores
`trap_capture_rate = 1.0` and `false_match_rate = 0.0`. It is saved by an
unrelated rule: at T1 both bank lines propose `setl_M2`, the contest rule sees
one settlement wanted by two subjects and declines both; at T2 both propose
`setl_K9` and the same thing happens; T3 is cardinality-agnostic and finds the
ambiguity honestly. The end-to-end metric never moves.

So the bug is real and it produces a false match — but it is **invisible to the
headline number on the shipped fixture**. `tests/matcher/test_tiers.py:136` uses
**one** subject deliberately for exactly this reason, and says so in its own
docstring: with two subjects the test passes on the broken implementation.

A note on the spec: the design spec this engine was built from stated at §7.2
that under the broken build "each tier would see a single candidate,
both bank lines would be matched, and `trap_capture_rate` would go to zero". The
mechanism it describes is right; the consequence is not, on this fixture. Run as
above, the contest rule masks it. The spec has not been corrected, and the
discrepancy is recorded here rather than smoothed over — it is the more
interesting fact, because it means a green end-to-end metric was never going to
be the thing that caught this.

So the rule, enforced in `core/matcher/tiers.py`:

> A subject's candidate set is **every** unclaimed settlement that satisfies the
> arithmetic and the date window, at any payment-leg count. The ambiguity rule
> is applied to that whole set. Only once a **single** candidate survives does
> its payment-leg count decide whether the match is labelled T1 or T2.

Structurally: `_ReconstructionTier._candidates` (:334) is *identical* for T1, T2
and T3 apart from the tolerance value. Cardinality lives in
`_accepts_cardinality` (:83), which `_Tier.match` calls only after
`len(candidates) > 1` has already been checked (:113) — that is, only on a set
of exactly one. `CandidatePool.payment_legs`'s docstring says it in the code:
*"This LABELS a match that is already unique — it must never filter a candidate
pool."*

There is a second-order version of the same failure, and it is guarded too: the
audit line emitted on an ambiguous subject prints the candidates' *actual*
payment-leg counts. An earlier version appended the tier's own label rule to
that sentence, which read as though the set had been filtered by it — the exact
opposite of what happened, and on the trap the counts differ so the sentence was
also false. Fixed in commit `2432b9b`, pinned by
`tests/matcher/test_tiers.py:303`.

---

## 6. The ambiguity rule

> **More than one valid candidate means match nothing.**

`core/matcher/tiers.py:113`. The subject gets `AMBIGUOUS_MULTI_CANDIDATE` and
the candidate set is recorded in the audit trail.

The mirror image is enforced too (`_Tier._resolve`): when one settlement is the
only candidate of two different subjects, matching either would make iteration
order the tie-breaker, so neither is matched. The same rule appears a third time
in order recovery (`CandidatePool._recover_order_refs`, `core/matcher/pool.py`),
which computes every leg's candidate set against an unmutated pool in pass 1 and
accepts only uncontested singletons in pass 2 — because a single pass that
claims as it goes lets the row read first win, which is statement order as
tie-breaker.

The reason is an accounting reason, not a software reason. **A false match is
worse than no match.** An unmatched bank line is an exception on a list that a
human works through; a wrongly matched bank line is two wrong ledger entries
that nobody is looking for. Guessing under ambiguity is precisely how false
matches are created, so the system declines and says so.

That is why `false_match_rate` and `trap_capture_rate` are reported next to the
headline match rate rather than below the fold. A match rate on its own is not a
result.

---

## 7. The verifier

`core/llm/verifier.py`. Pure deterministic Python, no matcher state, no engine,
no pool. Six checks over five frozen label spellings, in this order, and **all
must hold**:

```
existence -> exclusivity -> causality -> arithmetic -> coherence -> uniqueness
```

`CHECKS` at `core/llm/verifier.py:267`. `verify()` at :277 returns on the first
failure, and returns a verdict for every input including a malformed one — it
never raises on a bad hypothesis. That is the point: a crash in the verifier
would take a whole reconciliation run down over one bad LLM response.

### `existence` (`:85`)

**Rejects:** a null or unknown `proposed_bank_line_id`; a bank line with no
credit; a credit that is zero or negative; an empty proposal; unknown
transaction ids; a transaction id proposed more than once.

**Why it exists:** it runs first so every later check can index the context
without a `KeyError`. That guarantee only holds if the bank line is validated
here too — `_causality`, `_arithmetic` and `_uniqueness` all do
`ctx.bank_lines_by_id[h.proposed_bank_line_id]`, and `Hypothesis`'s field is
`str | None`. An invented id is exactly what a model produces.

Three of its clauses were added after demonstration, not in anticipation:

- A **debit-only** line read as `credit or 0` becomes a target of zero, which an
  empty proposal closes exactly. The prompt renders debit lines, so the analyst
  is invited to propose against them (`b218703`).
- A **zero or negative credit** is a zero-or-worse target: a settlement whose
  legs cancel closes it exactly. `BankLine` carries no non-negative validator
  and the ingest reader accepts `"0"` (`bc8dc05`).
- A **repeated id** passes every membership test trivially and is then counted
  again by `reconstruct` — naming one leg three times triples the net.
  `claimed_txn_ids` cannot catch this: it holds ids claimed by *previously
  accepted matches*, not ids repeated inside the hypothesis under test
  (`02645fe`).

### `exclusivity` (`:133`)

**Rejects:** a proposal naming a transaction already claimed by an accepted
match.

**Why it exists:** one PSP leg funds one bank line. Without it, one settlement
could be spent twice. The accept loop keeps `claimed_txn_ids` current *between*
verifications (`core/llm/pipeline.py`, rule 1) rather than verifying a batch
against one frozen context — see §7.1.

### `causality` (`:140`)

**Rejects:** a leg with `settled_at is None`; a leg whose `settled_at` is after
the bank line's `txn_date`.

**Why it exists:** money cannot arrive in the bank before the settlement that
produced it, and money that never settled at all cannot have funded a credit
either. `settled_at` is `date | None` on the frozen model and unsettled rows are
live in shipped data — `pay_1105` in `fixtures/tiny/` is one. Treating `None` as
"not late" waves through precisely the leg that provably did not fund the line
(`d157b8d`).

*One asymmetry, stated because it is real — and load-bearing.* `causality`
bounds lateness only: it rejects `settled_at > line.txn_date` and imposes no
lower bound. T1, T2 and T3 apply a **symmetric** ±2-day window
(`CandidatePool.within_window`, `WINDOW_DAYS = 2`). So a settlement that settled
thirty days *before* a bank line passes `causality`, where the deterministic
tiers would have refused it on the window.

This paragraph previously called the gap "latent rather than live" on the
grounds that no shipped fixture reached it. **That is no longer true**, and it
was the obfuscated-reference work that changed it:
`inject_obfuscated_settlement_ref` posts its bank line 4–9 days late *on
purpose*, precisely because the date window is the only space in which an
analyst layer can operate at all. The gap is
live, deliberate and exercised by `tests/llm/test_obfuscated_ref.py`.

Closing it was implemented and measured during defect close-out, then reverted
— defect close-out **D1**. Applying the tiers' window to
`_causality` takes **13 tests red** across `tests/llm/test_pipeline.py` and
`tests/llm/test_obfuscated_ref.py`, and the cause is structural rather than
fixture-deep: with the window applied, the verifier's admissible band becomes a
strict subset of what T3 already matches deterministically, so every subject the
analyst could still be asked about is one T3 either resolved already or declined
for an ambiguity `uniqueness` declines too. The analyst layer would be left with
no admissible input.

What was actually wrong here was the *claim*, not the code.
`core/llm/verifier.py` said the verifier is "exactly as forgiving as the loosest
deterministic tier and no more"; that is true of the ±100 paise amount tolerance
and false of the date window, and the comment now scopes itself accordingly. The
two paths are **not ordered by permissiveness in either direction**: the
verifier is wider on the date and strictly narrower on structure, because
`coherence` requires a proposal to be one complete settlement (no tier does) and
`uniqueness` counts competing candidates across the whole file rather than
within a two-day window (so it counts strictly *more* competitors than T3's
ambiguity rule). The tiers need a date window because they identify a settlement
by its amount alone; the verifier identifies one by structure and does not.
Pinned as intended by `test_a_stale_settlement_passes_causality_by_design`.

### `arithmetic` (`:162`)

**Rejects:** a proposal whose reconstructed net differs from the bank credit by
more than 100 paise.

**Why it exists:** this is the re-check the whole project's claim rests on. It
calls `reconstruct` — the matcher's own function, imported rather than
re-implemented, so the two can never disagree. The tolerance is T3's, the
loosest deterministic tier's: an LLM proposal does not get a wider amount window
than deterministic code would have taken.
`tests/llm/test_verifier.py:429` pins the boundary from both sides.

### `coherence` (`:202`) — reports under the `existence` label

**Rejects:** a proposal that is not exactly one settlement — legs spanning two
settlements, legs carrying no `settlement_id`, a settlement not in the ingested
data, or an incomplete leg set of a real settlement.

**Why it exists — and it exists because of a demonstrated exploit.** The first
four checks each test the proposed set *in isolation*. `uniqueness` enumerates
whole entries of `txns_by_settlement`, but it never asks whether the **proposed**
set is one of them. So a cherry-picked leg set assembled across settlement
boundaries — one that happens to sum to the bank credit — sails through with
zero or one closer. Two mutually exclusive hypotheses were accepted for one bank
line. Unconstrained subset-sum over the leg pool is not reconciliation.
`d157b8d`; pinned by `tests/llm/test_verifier.py:264`.

It runs *after* `arithmetic` deliberately: a proposal that is both incoherent
and arithmetically wrong should be reported as wrong, which is the more useful
diagnosis.

One hardening detail worth naming, because it is the difference between failing
closed and failing open: the expected leg set is derived from `ctx.txns_by_id`,
**not** from `ctx.txns_by_settlement`. The map answers only "was this settlement
ingested"; it is not the authority on what the settlement contains. A caller
that built it from *unclaimed* legs would otherwise present a partly-claimed
settlement as a complete one, and the remainder would be laundered into a whole
settlement. An empty map fails closed and loudly; a partial one fails open
silently (`ec2ac0e`, pinned by `tests/llm/test_verifier.py:367`).

**Why it reports `existence` and not `uniqueness`:** `CheckName`
(`core/llm/verifier.py:52`) is set-identical to `ReconException.failed_check`
(`core/models.py:101`) on the frozen contract, so a sixth spelling would be a
contract change that halts every lane. `existence` is the honest one of the five
— the settlement the hypothesis implicitly names does not exist as proposed.
`uniqueness` is the wrong label because the UI pins that value to the sentence
"more than one candidate settlement satisfied the arithmetic", which is false of
a cherry-picked leg set. So the tuple at :267 has six entries over five
spellings, and says so in a comment.

### `uniqueness` (`:175`)

**Rejects:** a proposal where more than one unclaimed settlement closes the same
bank line within the same ±100 paise.

**Why it exists — also because of a demonstrated exploit.** On an ambiguous pair
the two competing candidate sets are **disjoint** and neither has been claimed,
so `exclusivity` never fires. Every id exists. Both close arithmetically. Both
settle on or before the bank date. **All four earlier checks pass on both
hypotheses, and both get accepted** — `trap_capture_rate` silently goes to zero
and `false_match_rate` rises, only when `--llm` is on.

It is the same ambiguity rule the deterministic tiers obey. An LLM must not be
permitted to resolve what deterministic code correctly refused. Expressing it is
why `VerifyContext` carries `txns_by_settlement` at all; populated as `{}`,
`uniqueness` would pass vacuously on every hypothesis with no test anywhere
going red, which is why the accept loop builds it from every ingested row and
the docstring says so at length. Pinned by `tests/llm/test_verifier.py:197`,
`:213` and `:455`.

It runs **last** because it is the only check not about the proposed set at all,
and there is no point enumerating alternatives to a hypothesis that already
failed on its own terms.

### 7.1 Three rules the accept loop owns

`core/llm/pipeline.py` exists as `core/` rather than as a block inside
`api/jobs.py` for one reason: there must be exactly **one** accept loop. A
second caller assembling `VerifyContext` by hand is a second place these can be
got wrong, and each was demonstrated by exploit before it was written down.

1. **`claimed_txn_ids` is updated between verifications; nothing is batched.**
   `uniqueness` asks how many settlements close a given bank line. Nothing asks
   how many bank lines one settlement closes — that dual becomes live the moment
   a batch of hypotheses is verified against one frozen context. Two hypotheses
   naming the same settlement for two different lines would both pass. So the
   loop verifies one, accepts it, adds its legs to the claimed set, and only
   then looks at the next. `tests/llm/test_pipeline.py:252`, `:281`.

2. **`subject_id` is tied to `proposed_bank_line_id` before acceptance.** The
   verifier cannot know a subject's type, so it never checks this. A hypothesis
   carrying `subject_id="BL-TRAP"` and `proposed_bank_line_id="BL-REAL"`
   verifies clean and would credit a resolution to a subject the data does not
   determine — moving `trap_capture_rate`. `_subject_tie`
   (`core/llm/pipeline.py:250`) is that check. It is a seventh gate on the
   accept path, outside the verifier's six, and it also reports `existence` for
   the same frozen-contract reason. `tests/llm/test_pipeline.py:316`, `:344`,
   `:371`, `:393`.

3. **`txns_by_settlement` is built from every ingested transaction**, not from
   the unclaimed ones. Exclusion belongs in `claimed_txn_ids`, in one place.
   `tests/llm/test_pipeline.py:426`.

**A rejected hypothesis is a feature, not a failure.** It is the visible
evidence that the guardrail fires. The proposal text, the free-text reason and
the machine-readable `failed_check` all survive onto the exception the UI
renders, and `llm_rejection_rate` reports the count explicitly.

### 7.2 One rough edge in the prompt — closed

`_analyst_context` (`core/llm/pipeline.py`) filters candidate settlements *per
settlement, not per leg*, on the stated reasoning that showing half of a
partly-claimed settlement would invite a rejection the prompt manufactured
itself. It **used to** then append every leg with `settlement_id is None` under
a heading of its own, `## PSP legs with no settlement id`. But `coherence`
rejects any proposal whose legs carry no `settlement_id` — so no proposal
containing one of those legs could ever be accepted. The prompt rendered a
category of row guaranteed to be rejected, which is the same self-inflicted
rejection the per-settlement filter was written to avoid, two sections further
down the same prompt. Harmless to correctness (the checks held), but it inflated
`llm_rejection_rate` for no information gain.

**Fixed in defect close-out.** `_analyst_context` no longer collects those legs
and `render_prompt` no longer has the section; orders reachable only through
such a leg leave scope with it. They are not rendered as context-only material
either — every row in the prompt is one the model may name in a proposal, and a
section headed "here is data you may not use" spends the same tokens to buy a
rule the model can misread, where dropping it spends none and cannot be
misread. Both halves are pinned:
`test_the_prompt_does_not_invite_proposals_it_must_reject` (the renderer, which
stays defensive for any context a future caller assembles) and
`test_the_analyst_context_offers_no_settlement_less_legs` (the accept loop).

One neighbour is deliberately **not** closed: a candidate settlement containing
a leg that never settled is still rendered in full, and `causality` will reject
any proposal naming it. Unlike the settlement-less legs, those rows cannot
simply be dropped — `coherence` requires the *complete* leg set, so a settlement
shown with a leg missing would be worse than one shown whole. Removing the
settlement from the candidate list entirely is the real fix and is a judgement
about candidate selection rather than about rendering — defect close-out D4.

---

## 8. The three-module separation

```
  core/generator/  ──emits──>  orders.csv  psp.csv  bank.csv   ──>  core/matcher/
        │                                                              │
        └──────────emits──────>  truth.json  ─────────┐                │
                                                      v                v
                                                   scorer/  <── MatchResult
```

- **`core/generator/`** writes the CSVs *and* `truth.json`, which records the
  linkages, every injected defect and the `unresolvable_ids`.
- **`core/matcher/`** must be unable to read `truth.json`. It consumes only the
  three CSVs.
- **`scorer/`** reads `truth.json` and compares. It is never imported by the
  matcher; the dependency runs the other way (`scorer/score.py` imports
  `core.matcher.engine`).

That one-directional dependency **is** the credibility argument for every number
this project reports. `tests/test_boundaries.py` enforces the matcher end of it
statically:

- `test_matcher_cannot_see_ground_truth` (`:106`) — walks the AST of every file
  under `core/matcher/` and asserts no `ast.Import` or `ast.ImportFrom` names a
  module containing `generator`, `scorer` or `truth`. It handles bare relative
  imports (`from .. import generator` binds the name with no module qualifier at
  all) by checking `node.names` as well as `node.module`.
- `test_matcher_does_not_open_truth_files` (`:117`) — asserts no string literal
  under `core/matcher/`, including f-string parts, contains `"truth"`.
  Module/class/function docstrings are exempt **by node identity**, not by
  value, so an ordinary literal that happens to share a docstring's text is
  still scanned.

### Its documented limits, honestly

The test file's own header says what it cannot do, and it is worth repeating
here rather than letting a reader discover it:

- **It does not catch dynamic imports.** `importlib.import_module("core.generator.rng")`
  and `__import__("core.generator")` are `ast.Call` nodes, not `ast.Import` /
  `ast.ImportFrom`. The import walker never sees them.
- **It does not catch a runtime-assembled path.** `Path("fixtures") / f"{name}.json"`
  where `name` comes from config or an environment variable has no literal
  `"truth"` substring to inspect.
- **Comments are out of scope** — the check walks the AST, and comments never
  become AST nodes.

This is a **good-faith structural proof against accidental and lazy coupling**:
the careless import, the debug call to a truth fixture that never got removed.
It is not an adversary-proof sandbox, and it does not claim to be. If any of
those gaps were ever exploited, the fix belongs in code review, not in a more
elaborate static check that will always have its own gap one level further out.

Two further scope facts, stated because omitting them would overclaim:

- The truth/generator/scorer ban covers **`core/matcher/` only**. `core/llm/`,
  `core/ingest/`, `core/canonicalize/` and `core/audit.py` are not covered by
  it. Since an accepted LLM hypothesis produces a `MatchGroup` the scorer
  grades, the LLM package is inside the credibility perimeter in substance but
  outside the static check in form. That is a gap.
- `scorer/metrics.py`'s docstring says *"the only module in the repository that
  opens the ground-truth file"*. That is not quite right:
  `api/jobs.py::dataset_facts` also opens `truth.json`, to read `seed` and
  `record_count` for the run summary. It never reads `linkages` or
  `unresolvable_ids`, so the grading path is unaffected — but the sentence as
  written is stronger than the code.

---

## 9. Layering

```
  web/     Next.js (App Router), TypeScript, Tailwind, shadcn/ui
           no business logic; one API boundary module (web/lib/api.ts);
           one money formatter (web/lib/money.ts). No chart library.
    |  HTTP + JSON only. web/ never reads core/models.py; the contract is
    |  api/openapi.yaml, from which web/lib/api-types.ts is generated.
    v
  api/     FastAPI. 19 paths, 21 operations. Thin.
           Validate -> call core/ -> 404 a missing row -> serialise. No
           arithmetic: net, fees, tax and every rate arrive already computed
           and are passed through untouched.
           auth.py issues and reads the session; deps.py binds the org to the
           repository; ingest.py is the upload pipeline.
    |
    v
  core/    Pure Python. Zero web dependencies. No wall clock.
           models / ingest / canonicalize / matcher / llm / generator / audit
           adapters / itc / drift
    |
    v
  core/store/   SQLite via SQLModel. Persists what it is handed; computes
                nothing; reads no clock. Every read filters on org_id.
                blobstore.py holds uploaded bytes, encrypted when configured.
```

Two invariants are enforced by tests rather than trusted:

**1. `core/` imports no web dependency.**
`tests/test_boundaries.py::test_core_has_no_web_dependency` (`:128`) walks every
`.py` under `core/` and asserts no import starts with `fastapi`, `uvicorn` or
`starlette`. This matters because `core/store/repo.py` lives under `core/` and
is the file most tempted to reach for `fastapi.Depends` or `HTTPException`; it
returns plain models and raises plain exceptions instead, and `api/` turns those
into HTTP.

**2. `core/` calls no wall clock.**
`tests/test_models.py::test_no_module_under_core_reads_a_wall_clock` (`:187`)
walks every `.py` under `core/` and asserts no call to `datetime.now`,
`datetime.utcnow`, `datetime.today`, `date.today`, `time.time` or
`time.monotonic`.
`tests/api/test_store.py::test_the_store_never_reads_a_clock` (`:59`) is the
narrower value-level check over `core/store/`, adding `perf_counter`.
`tests/test_models.py:168` asserts `RunSummary.created_at` has no default and no
`default_factory` — a later "helpful" `default_factory=datetime.now` would move
the clock back into `core/`, and that test is the tripwire.

Timestamps are stamped at the API boundary: `api/jobs.py::utc_now` is the only
place a run's `created_at` comes from, and the run is timed there with
`perf_counter`, with the elapsed seconds handed to `scorer.score` rather than
measured inside it. Audit ordering uses a monotonic `sequence` int
(`core/audit.py`), and `entry_id` is derived from `run_id` and `sequence` rather
than `uuid4`, so replaying a run reproduces the log byte for byte.

The engine timer stops **before** the analyst runs
(`api/jobs.py`, `elapsed = time.perf_counter() - started`), because
`throughput_records_per_sec` is defined as excluding LLM latency — otherwise the
same engine would report a different speed depending on a flag that does not
touch it.

---

## 10. The adapter layer — a merchant's file becomes a canonical record

`core/ingest/` reads the three canonical CSVs and nothing else. That was the
whole input surface until the system had to accept a file somebody exported, and
the reason it is still true is that the adapters sit **in front** of it rather
than inside it: an adapter's only job is to turn one real layout into the same
typed records `core/ingest/` would have produced, and everything below that
point cannot tell which door the records came through.

Seven formats, one Protocol (`core/adapters/base.py`):

| `format_id` | Reads | Produces |
|---|---|---|
| `razorpay-settlement-v2` | PSP settlement report, per-transaction | `PSPTransaction` |
| `bank-csv-hdfc-v1` | HDFC net-banking CSV | `BankLine` |
| `bank-csv-icici-v1` | ICICI net-banking CSV | `BankLine` |
| `mt940-v1` | SWIFT MT940 statement | `BankLine` |
| `orders-csv-shopify-v1` | Shopify order export | `Order` |
| `cod-remittance-delhivery-v1` | courier COD remittance | `Order` + `PSPTransaction` |
| `slice-pdf-v1` | Slice small-finance-bank statement **PDF** | `BankLine` |

**`slice-pdf-v1` is the only one of the seven whose layout was read off a file a
real person's bank actually produced, and the only one that is not a text
export.** Both facts change the shape of the adapter, so §10.1 is about it
specifically. It is also the only one whose evidence class is *verified against
a genuine artefact* rather than written from a published schema.

**Detection is by header shape, with a threshold and two refusals.**
`core/adapters/registry.py` reads `SNIFF_BYTES` of the file, asks every
registered adapter for a confidence, and takes the best. It refuses when nothing
clears `DETECTION_THRESHOLD` (0.60), and it refuses when the top two tie. Both
refusals are the feature. A file whose header half-resembles three layouts is
not a file to guess at, and two adapters equally sure is either a registry bug
or a genuine ambiguity — in both cases the correct output is a question rather
than a parse. The error carries every candidate and its score, so "why did it
not read my export" is answered in the message rather than in a debugger.

**A file that is not text at all raises a different exception.**
`UndecodableFileError` is not folded into the zero-confidence report, because a
spreadsheet renamed to `.csv` is a different fact from a CSV missing a column,
and the two have different fixes. A report of seven zeroes would imply seven
adapters looked at the bytes and declined; none of them did.

**Except that one format is genuinely binary, and the registry now knows it.**
A PDF statement is not text and never will be, so "these bytes do not decode"
stopped being the same fact as "this file is not a statement" the moment
`slice-pdf-v1` landed. An adapter sets `reads_binary = True` when it reads a
container rather than an export, and only those adapters are shown a sniff
prefix that failed to decode. If none of them recognises it, the original
`UndecodableFileError` is raised exactly as before — so a spreadsheet renamed to
`.csv` gets the answer it always got, and a text-only adapter is still never
handed a NUL byte to sniff.

**Row damage is quarantined, never dropped and never guessed at.** `parse` does
not raise past a row: a row it cannot read becomes a `QuarantinedRow` carrying
the raw text, the line number an editor shows, and a typed `QuarantineReason`
(`BAD_DECIMAL`, `TRUNCATED_ROW`, `AMBIGUOUS_DIRECTION`, …). The alternative —
failing the whole file — throws away the ninety-nine rows that were fine; the
other alternative, coercing the bad row, puts a number nobody entered into a
ledger.

**The adapters cannot see the generator, and that is enforced.**
`tests/adapters/test_adapter_boundaries.py` asserts that no module under
`core/adapters/` and no test under `tests/adapters/` imports `core.generator`.
An adapter that could reach the generator could be written against the
generator's own output rather than against a published schema, and its fixtures
would stop being independent evidence. What each layout's evidence actually is
differs format by format, and three of the seven are `FROM KNOWLEDGE` — the adapters exist and are fixture-tested; the *layouts* are
not all verified, and the two facts have to be read together. Exactly one is
`VERIFIED AGAINST A GENUINE ARTEFACT`, and it is `slice-pdf-v1`.

**The exporter is the inverse, and that is what lets ground truth reach the
adapters at all.** `core/generator/export.py` writes the same seeded dataset out
in three of these layouts (`--export-as razorpay`), and `tests/round_trip/`
reads it back through the registry and scores it against the same `truth.json`.
The metrics come back byte-identical. That proves the reader inverts the writer;
it does **not** prove the layout is right: a round trip through this project's
own writer can only ever be as correct as the writer. One canonical field has no home in the HDFC layout at all —
`BankLine.utr` — and the export asserts the loss rather than approximating it.

---

### 10.1 `slice-pdf-v1` — the PDF, and the two stages

Every other adapter reads a delimited text export, where a row is a line and the
columns are punctuation. A PDF has neither. What a PDF's text layer gives back
is one string per page in which a five-column visual row has been flattened into
one to four lines, wrapped at a fixed character width. So this adapter is shaped
differently, in two ways that are worth stating because both were decisions.

**Two stages, and the split is what makes it testable.** `extract_pages` turns
a PDF into one string per page and is the only place that names pypdf — imported
lazily inside the function, so `core/` stays as cheap to import as it was.
`parse_text` is a pure function from text to records: no filesystem, no clock,
no PDF. Every decision the format makes lives in the second stage, so the
committed fixtures are `.txt` files of *extracted text* and **no PDF-writing
dependency enters the project at all**. The extract stage is still covered — by
a PDF assembled byte by byte inside the test module, which costs forty lines of
PDF syntax and buys not taking a library on to produce test input.

**Rows are delimited from both sides, and neither side alone is enough.** A row
starts at a date at line start and ends when the accumulated text ends in its
amount-and-balance pair. The start rule alone would read the page header as a
row on every page, because the header's first line is the statement period —
`DD Mon 'YY - DD Mon 'YY`, which begins with a date. The end rule alone has
nothing to attach a row to. Together they also survive the harder case: a
narration that wraps onto a line beginning with something that reads as a date.
A date starts a row only when no row is *pending*, and rows terminate eagerly at
their money pair, so a pending row is by construction incomplete and the date is
a wrap. That is a deliberate correct-rather-than-quarantine choice; the reverse
rule is the one that hides, because the fragment it emits carries the real row's
amount and balance and the balance chain would still close.

**A continuation is glued on with no separator.** The narration wraps mid-token
— mid-VPA, mid-word — so a space inserted at the wrap would corrupt every
wrapped narration in the file. The line carrying the reference and the money is
a different thing: it is the row's remaining *columns*, not a wrap, so it is
recognised by shape and joined with a space.

**The balance chain is this format's arithmetic check, and it is doing more work
than the settlement one.** For `razorpay-settlement-v2`, `ARITHMETIC_MISMATCH`
catches a row whose own columns disagree. Here it catches a *reconstruction*: a
mis-joined row takes an amount or a balance from the wrong place, and the chain
is what notices. A statement that closes end to end is a statement whose wrapped
lines were put back together correctly — which is how a strong claim can be
made about a file whose rows are never disclosed.

---

## 11. The upload path — the front door, and the order its steps are in

`api/ingest.py` is four steps, and the order is the design:

```
  bytes ──> 1. hash ──> 2. detect ──> 3. parse ──> 4. store blob, then rows
              │             │            │
              │             │            └─ a row that fails becomes a quarantine row
              │             └─ no adapter, or a tie -> 422 with every candidate's score
              └─ already held by this org? -> return the existing id, do nothing else
```

**Hashing first is what makes a re-upload cost one `SELECT`.** The content
digest is computed before anything else and `Repo.upload_for_content` is asked
whether this org already holds it: no temporary file, no sniff, no parse, no
second blob. Doing the lookup last — after a parse whose output is then
discarded — satisfies the letter of idempotency and none of the point.

**The idempotency check lives in the repository, not in the route.** A route
that looked the hash up, found nothing, and then inserted has a window in which
a second request inserts the same file, and the merchant ends up holding two ids
for one document — precisely what content addressing exists to prevent.
`Repo.record_upload` returns `UploadIngestion(upload, already_ingested)` and
closes it.

**The dedup key is `(org_id, content_sha256)`, never the hash alone.** Two
tenants uploading the same bytes get two uploads. Handing the second tenant the
first one's id would tell them that org holds that document, and would then
serve them its quarantine rows — a tenancy leak wearing deduplication's clothes.

**A refused file is not retained.** There would be no upload row pointing at it,
so it could never be listed, reviewed or erased, and an unreferenced copy of a
merchant's data with no way to find it again is the one thing a retention policy
cannot describe. The 422 already carries everything the file could have told
them.

**A run over uploads is the same run.** `POST /api/runs` takes `{dataset_id}`
**or** `{upload_ids}`, exactly one; below the handler they are one function
(`api/jobs._execute`), so the matcher, the analyst layer and the scorer cannot
tell which door the records came through. There is deliberately no `source`
discriminator — it would be a third field a client could set inconsistently with
the other two.

`Repo.upload_inputs` returns records **in the order the files carried them**
(surrogate key within an upload, `(uploaded_at, upload_id)` across uploads).
Sorting by record id instead reordered `MatchGroup.psp_txn_ids`, and the two
paths then disagreed on a field that is on the wire, in the audit trail and on
the screen. The ordering is a property of the store rather than of the request,
so the same uploads selected in a different order still feed the engine the same
sequence.

**An upload run reports `metrics: null` and `seed: -1`, permanently.** There is
no ground truth for a merchant's own files — every rate in `Metrics` is measured
against the generator's record of which order settled into which batch and
landed on which bank line — so a rate here would be a number grading itself. And
nothing generated the records, so there is no seed: `RunSummary` is frozen with
a non-nullable `seed`, and `-1` is a value no caller could have supplied,
because the generator refuses negatives. Both are sentinels, both are recorded
as such at all four call sites, and the console has a third empty state for
them, distinct from "still executing" and from "failed".

---

## 12. Tenancy, and why the org never reaches a route handler

`core/store/repo.py` binds a `Repo` to one `org_id` at construction. Every
insert stamps it and every read filters on it (`Repo._mine`), and **no method
accepts one** — so no caller above that layer can widen its own scope. The org
reaches the store through exactly one place, `api/deps.get_repo`, which reads it
off the session principal.

The consequence is a claim that can be checked mechanically rather than
reviewed: `api/routes.py` does not contain the string `org_id` anywhere.
`tests/api/test_tenancy.py::test_no_route_handler_mentions_an_org_id` asserts it
against the module's own AST, which is the only form of the claim that survives
a future author who has not read this document.

The cross-org proof is parametrised over the **15 read operations** the contract
declares, from a table *derived from `api/openapi.yaml`* — adding an operation
to the contract without adding it to the walk fails the suite, which is the same
enforcement idea as `tests/test_briefs.py`. Org B receives **404, never 403**,
for org A's resources: "exists but is not yours" and "does not exist" have to be
indistinguishable, or the API answers "which run ids do other tenants hold" for
anyone patient enough to enumerate.

**Authentication is off by default, and disabled is not a degraded mode.**
`RECON_AUTH` unset is byte-for-byte the behaviour this API had before any of it
existed: no cookie, no 401, one implicit org. That default is load-bearing
rather than convenient — a tenancy filter that is bypassed when auth is off is a
filter nobody exercises, so single-user mode maps to `DEFAULT_ORG_ID` and goes
through exactly the same code. The configuration this system actually runs in is
the one that tests the control.

**Audit-on-read is owned by the middleware, not by the handlers**
(`api/main.py`). Every `GET` under `/api/` appends one `access_log` row: who,
which resource, which id, when, and what status. A per-handler call would be one
chance to forget per endpoint, and would leave the next endpoint silently
unlogged. The resource recorded is the route template rather than the URL,
because `/api/runs/{id}` groups and `/api/runs/run-9f3c…` does not. Refused
reads are logged too, against `anonymous` when no usable session was carried,
because a 404 sweep across run ids is what enumeration looks like from the
inside and a log of successful reads only would be blind to precisely the
behaviour the control exists to catch. It fails closed: a read that cannot be
logged is not served.

---

## 13. The blob store

`core/store/blobstore.py` holds uploaded bytes. Two properties, and the second
is what makes the first safe.

**Content-addressed on the plaintext.** A blob's address is the SHA-256 of its
*plaintext*, so the same file stored twice is stored once and the second `put`
is a no-op. Addressing the ciphertext instead would have been the obvious choice
and would have been wrong: AES-GCM uses a fresh nonce per encryption, so the
same file encrypted twice produces two different ciphertexts, and deduplication
would have disappeared the moment encryption was switched on — quietly, as a
storage-cost regression nobody attributes to the security work.

**Encrypted at rest, with the address bound in.** The digest travels as AES-GCM
additional authenticated data, so a blob's ciphertext cannot be moved to another
blob's address: the tag stops verifying and `get` raises rather than returning
the wrong merchant's file under the right name. Confidentiality without that
binding still lets an attacker with write access to the directory swap two files
around, which for a reconciliation input is a perfectly good attack.

**Plaintext is a mode, not a fallback.** No key means no encryption, and the
envelope records which it was in a byte on disk. The two modes refuse each
other's blobs — a keyed store will not read a plaintext blob and a keyless store
will not read a ciphertext one, and both raise. What must never happen is a
store that was asked to encrypt and quietly did not. `RECON_BLOB_KEY` chooses;
Which mode a given deployment runs in is a deployment decision, and
`tests/api/test_blobstore.py::test_encryption_is_actually_on_in_this_build`
asserts the encrypted path is the one taken when a key is set.

The key is held on the instance and appears in no `__repr__`, no exception
message and no path. And no clock is read here, because `core/` may not: a blob
carries no timestamp of its own, and the one beside it is stamped at the API
boundary like every other timestamp in this system.

---

## 14. Results

Deterministic path only (no LLM), on this machine, best of three runs. The
generator is seeded and byte-reproducible, so any row of this table can be
reproduced from scratch:

```
python -m uv run recon generate --seed 42 --count 5000
python -m uv run recon run --dataset fixtures/seed42-5000 --no-llm
```

(`fixtures/tiny/` is hand-written and committed; `seed42-50` and `seed42-500`
are committed; `seed42-5000` is 1.7 MB and may not be, so regenerate it with the
command above.)

| Dataset | orders | psp rows | bank lines | T0 | T1 | T2 | T3 | matches | exceptions |
|---|---|---|---|---|---|---|---|---|---|
| `fixtures/tiny` | 12 | 27 | 6 | 2 | 0 | 1 | 1 | 4 | 3 |
| `fixtures/seed42-50` | 50 | 85 | 17 | 10 | 1 | 0 | 1 | 12 | 6 |
| `fixtures/seed42-500` | 500 | 852 | 171 | 126 | 1 | 9 | 5 | 141 | 40 |
| `fixtures/seed42-5000` | 5000 | 8451 | 1677 | 1227 | 22 | 78 | 50 | 1377 | 400 |

The `exceptions` column is every exception, bank-line and PSP-side. The
bank-line share is 2, 5, 30 and 300; the rest are suppressed duplicate PSP rows,
which are diagnostics and sit outside every rate's denominator.

| Dataset | auto_match_rate | recall_on_resolvable | **false_match_rate** | **trap_capture_rate** | exception_rate | records/sec |
|---|---|---|---|---|---|---|
| `fixtures/tiny` | 1.000 | 1.000 | **0.000** | **1.000** | 0.333 | — |
| `fixtures/seed42-50` | 0.800 | 0.800 | **0.000** | **1.000** | 0.294 | 31,706 |
| `fixtures/seed42-500` | 0.876 | 0.876 | **0.000** | **1.000** | 0.175 | 37,160 |
| `fixtures/seed42-5000` | 0.873 | 0.873 | **0.000** | **1.000** | 0.179 | 29,730 |

The `records/sec` column is the one figure in these two tables that does not
reproduce on another machine — best-of-25 after 5 discarded warm-ups, which is the only
version of this number worth carrying. An earlier edition of this table quoted
5,760 rec/s at 5,000 records; that was the pre-index engine and the figure had
gone stale in place, which is exactly the drift this repository has been bitten
by before. Every other cell comes from the commands above and was re-derived on
2026-08-30.

Reading the table honestly:

- `tier_counts` always carries all five keys, zeros included. On
  `fixtures/tiny/`, T1 legitimately scores **0** — and that zero is a result,
  not a silence. "T1 matched nothing" and "we do not know what T1 did" are
  different claims (`core/models.py`, the `tier_counts` validator).
- The distribution is lopsided by construction: T0 carries most of it, because
  most generated bank lines carry a clean settlement reference. T2 — the tier
  that solves the actual problem shape — carries 78 of 1,377 at 5,000 records.
  Showing the small tiers rather than rounding them away is the same honesty
  argument as `false_match_rate`.
- The `exception_rate` denominator is `len(truth.linkages)`, taken from truth
  and never from the result being graded. An earlier version divided by
  `len(matched | excepted)`, so a subject the engine dropped from *both* sets
  left the denominator with it and the rate **improved** — an engine could raise
  its score by losing work (`03e683c`).
- Throughput used to degrade about 4× per record between 500 and 5,000, because
  `_ReconstructionTier._candidates` scanned every unclaimed settlement for every
  open bank line, once per tier, and nothing indexed settlements by net — 8M
  comparisons at 5,000 records, costing 0.87 s. The pool now builds that index
  once per run, keyed on the reconstructed net and **nothing else**, with
  `bisect` over the sorted nets for T3's ±100 paise window. 5,000 records cost
  0.17 s and 50,000 cost 1.99 s, where the scan needed 141 s. Per-record cost
  now rises ~1.2× per decade rather than ~3.5×.
- The index is keyed on the net alone on purpose, and that is a correctness
  constraint before it is a design note. Partitioning it by payment-leg count
  would make the T1/T2 label a tie-breaker: two settlements closing the same
  credit at different cardinalities would each look unique inside their own
  partition and both subjects would match, with every individual rule still
  reading as correct. For the same reason a lookup returns *every* settlement at
  a matching net rather than the first — more than one candidate means match
  nothing, so an early return would turn an ambiguity into a false match.
  `tests/matcher/test_index.py` runs the index against the scan it replaced;
  `tests/test_scale_acceptance.py` holds every metric at 50, 500 and 5,000
  records to the capture taken before the change.

**Every unmatched *resolvable* subject at every scale belongs to one of exactly
two defect classes.** Verified as set equality with no remainder at 50, 500 and
5,000 records: `split_settlement` accounts for 2, 10 and 100 bank lines, each
reported as `AMOUNT_MISMATCH`; `obfuscated_settlement_ref` accounts for 1, 10 and
100, reported as `ORPHAN_BANK_LINE`, `UNPARSEABLE_NARRATION` or
`NO_SETTLEMENT_REF` depending on how far the narration parsed. Nothing else is
unmatched — there is no residual category of failure hiding behind the number.

The two are different problems. `split_settlement` needs a deterministic tier
nobody has written and is unsolvable by the analyst too (§15).
`obfuscated_settlement_ref` is deterministically unsolvable *on purpose* — no
unclaimed settlement settles within the tiers' two-day window of those lines, so
the engine declines rather than guessing at a reference only a human can read
safely — and it is precisely what the analyst layer closes.
Both reproduce from the commands in §17.

---

## 15. The known limitation: split settlements

One settlement paid across two bank lines. From `fixtures/seed42-50/`:

```
BL-0007  2026-01-18  "...SETL setl_00006 PART 1 OF 2"  credit   2,510,744
BL-0018  2026-01-19  "...SETL setl_00006 PART 2 OF 2"  credit + 3,328,196
                                                                ─────────
                                                                5,838,940
setl_00006, 6 legs, reconstructs to                             5,838,940
```

It breaks the identity every other defect leans on. Every other settlement
satisfies `Σ legs == its bank line's credit`; this one satisfies
`Σ legs == credit₁ + credit₂` and matches **neither** line on its own. Both
lines name the settlement in their narration, so T0's reference clause hits and
its arithmetic clause declines; T1, T2 and T3 find no candidate within tolerance;
and `_classify` reports `AMOUNT_MISMATCH` with the residual delta on both lines.
`truth.json` marks it `resolvable: true` — the answer *is* derivable from the
CSVs alone.

**It is unresolved in both paths, and it was deliberately not implemented.**

*Deterministically* it would need a new tier: search subsets of bank lines whose
credits sum to a settlement's net. That is a new tier label, which is a change
to `MatchGroup.tier`, `TIER_KEYS` and `Metrics.tier_counts` — all frozen
contract.

*Via the LLM* it would need a hypothesis spanning two bank lines, and the
contract does not have the shape for one. `Hypothesis.proposed_bank_line_id`
(`core/models.py:127`) is a single `str | None`. `MatchGroup.bank_line_id`
(`core/models.py:71`) is a single `str`. There is nowhere to put the second
line. And `_subject_tie` requires `proposed_bank_line_id == subject_id`, so a
proposal that named one of the pair and closed against both would be rejected on
the tie even if the arithmetic were expressed.

**The reason it was left alone is the reason §5 exists.** A multi-line search is
exactly where a tie-breaker on an ambiguous set creeps back in: once you are
searching subsets, "the two-line combination that sums closest" is a
tie-breaker, and at 5,000 records there will be spurious pairs whose credits sum
to some unrelated settlement's net. Shipping a tier that resolves 100 split
settlements and invents even a handful of false matches would trade a
`false_match_rate` of exactly 0.000 for a bigger match rate, which is the wrong
trade in accounting and the wrong trade for this submission.

So it is reported as an exception with a machine-readable reason code, on a list
a human can work through, and named here.

---

## 16. How it was built

Five lanes — generator, matcher+scorer, LLM, API+store, web — implemented in
parallel in separate git worktrees (`E:/rp-wt/{generator,matcher,llm,api,web}`),
each coding against contracts frozen before any of them started
(`core/models.py`, `api/openapi.yaml`, and the CSV wire format), with no ability to
communicate mid-flight.

Two lanes (generator, then matcher+scorer) landed by fast-forward; the rest came
in as four merge commits — `942d305`, `deceea3`, `a0baf67`, `6444bb9` (web
merged twice, having continued past its first merge). **All four are
conflict-free, and you can check that rather than take it on trust:**
`git merge-tree --write-tree <parent1> <parent2>` on each of them re-derives the
merge's exact tree hash and prints no conflict output.

That is context for why the boundaries are clean, not an achievement in itself.
It is also *why* the contracts are so heavily commented: a frozen field with a
comment explaining what would break if you changed it is the only communication
channel five parallel lanes had.

---

## 17. Running it

`uv` is installed as a Python package and its shim is not on PATH; invoke it as
`python -m uv`.

```
python -m uv run pytest                                      # 1414 passed
python -m uv run recon run --dataset fixtures/seed42-500 --no-llm
python -m uv run uvicorn api.main:app --reload --port 8000
cd web && npm run dev                                        # http://localhost:3000
```

`README.md` carries the demo stack in full, including the one that bites: the
console's `NEXT_PUBLIC_*` variables are inlined at **build** time, so a build
made without them serves a bundle that answers from MSW while the API sits
idle.

One caveat on the CLI: `recon run --llm` **does not run the analyst**. It is
accepted, and `core/matcher/cli.py` prints a line pointing at the path that
does: the analyst layer has exactly one wiring point, `api/jobs.py::execute_run`,
reached via `POST /api/runs` with `use_llm: true` and `ANTHROPIC_API_KEY` set.
That is deliberate — one accept loop, one caller — and it also keeps
`core/matcher/` free of anything that would need to read `truth.json`.

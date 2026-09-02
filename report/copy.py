"""The methodology prose, in the one place it now lives.

Spec section 5. The console used to carry ~600 words of explanation per run
because there was nowhere else to put it; section 6 strips that prose out, and
this module is where it goes. Nothing here is invented for the report: every
paragraph is carried over from the console source that owned it, named beside
it, so a reader comparing the two finds the same argument rather than a second
one.

  * the four derivations           `web/lib/derivations.ts`
  * the tier ladder                `web/lib/tiers.ts`
  * what a confidence means        `web/lib/confidence.ts`
  * the standing limits            `web/app/runs/[id]/page.tsx` (StandingLimits)
  * the headline claim             `web/components/summary/HeadlineClaim.tsx`
  * the unscored-run explanation   `web/app/runs/[id]/page.tsx` (NoMetricsYet)
  * the analyst's five states      `web/lib/analyst.ts`

**THIS FILE ASSERTS NO FIGURE.** Not a rate, not a confidence, not a tier
count. Every number in the rendered document is read off the `RunSummary`,
`Metrics`, `MatchGroup` and `ReconException` objects handed to `build_report`.
A constant here that happened to be a number would be the exact defect this
report was written to answer: `lib/tiers.ts` once carried a hardcoded copy of
the tier confidences and rendered the word "verified" on the rung the engine
stamps 0.70, and nothing bound the two together, so nothing caught the drift.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: `RunSummary.seed` for a run whose inputs a merchant uploaded. Mirrors
#: `api/routes.py:UNSEEDED_RUN`, and is duplicated rather than imported because
#: `report/` is a pure library that `api/` imports, never the reverse. -1 is not
#: a seed any caller could have supplied -- the generator refuses negatives --
#: so it is testable, and "Seed -1" is never printed as though it were one.
UNSEEDED_RUN = -1

TIER_KEYS = ("T0", "T1", "T2", "T3", "LLM")


# --- the four metrics -------------------------------------------------------


@dataclass(frozen=True)
class Derivation:
    """What one number counts, and what it does not prove."""

    key: str
    label: str
    definition: str
    numerator: str
    denominator: str
    caveat: str


#: The four the run is entitled to quote, in the order they are read in. The
#: order IS the argument (HeadlineClaim.tsx): what was resolved, what was
#: resolved correctly, what was asserted wrongly, and what was correctly
#: declined. None of them is a result on its own.
DERIVATIONS: tuple[Derivation, ...] = (
    Derivation(
        key="auto_match_rate",
        label="Auto-match rate",
        definition=(
            "The share of subjects the data determines that the deterministic "
            "tiers resolved, with no model involved."
        ),
        numerator=(
            "subjects matched by T0-T3, intersected with the resolvable set"
        ),
        denominator="resolvable subjects",
        caveat=(
            "A cardinality-filtered candidate pool - the exact defect the "
            "ambiguity trap exists to catch - leaves this number byte-identical "
            "at every scale. It moved no digit when the bug was injected "
            "deliberately. Read it knowing the scorecard is demonstrably blind "
            "to at least one real correctness bug; the unit tests are what "
            "catch that one."
        ),
    ),
    Derivation(
        key="recall_on_resolvable",
        label="Recall on resolvable",
        definition=(
            "The share of subjects the data determines that were matched "
            "CORRECTLY - checked against the recorded linkage, not merely "
            "matched."
        ),
        numerator=(
            "matches that agree with the recorded linkage, intersected with the "
            "resolvable set"
        ),
        denominator="resolvable subjects",
        caveat=(
            "Its agreement with the auto-match rate is a checkable invariant, "
            "not a coincidence: the two are numerator-different - matched, "
            "versus matched correctly - and coincide only while no false match "
            "exists. A divergence is the signal that the engine has produced a "
            "wrong match; check that before reading anything else."
        ),
    ),
    Derivation(
        key="false_match_rate",
        label="False-match rate",
        definition=(
            "The share of the matches this run asserted that disagree with the "
            "recorded linkage. A wrong match is worse than no match: an "
            "unmatched line is an exception a human works through, a wrongly "
            "matched one is two bad ledger entries nobody is looking for."
        ),
        numerator="matches disagreeing with the recorded linkage",
        denominator="all matches produced (match_count, and this one IS on the wire)",
        caveat=(
            "With no matches at all the denominator is empty, this evaluates to "
            "zero, and precision reads 1.0. An engine that matches nothing "
            "scores perfectly on both. It means something only beside the "
            "auto-match rate and recall."
        ),
    ),
    Derivation(
        key="trap_capture_rate",
        label="Trap capture rate",
        definition=(
            "The share of subjects that are unresolvable BY CONSTRUCTION - two "
            "settlements with an identical net on an identical date, where "
            "either assignment satisfies the arithmetic - that the engine "
            "correctly left unmatched."
        ),
        numerator="unresolvable subjects correctly left unmatched",
        denominator="unresolvable subjects (Metrics.total_traps)",
        caveat=(
            "An engine that matches nothing scores 1.0 here - it leaves the "
            "traps alone by leaving everything alone. Confirmed, not "
            "hypothesised. And uniquely among these rates, an EMPTY denominator "
            "evaluates to 1.0 rather than 0.0, because declining a trap that "
            "does not exist is not a failure. Both facts make this a necessary "
            "condition and not an achievement."
        ),
    ),
)

#: Why the four are quoted together or not at all (HeadlineClaim.tsx).
WHY_ALL_FOUR = (
    "None of those four numbers is a result on its own. An engine that matches "
    "nothing scores a false-match rate of zero and a trap-capture rate of 1.0 - "
    "that is measured, not hypothetical. Quote all four or none."
)

#: The subject every rate on this run counts (derivations.ts).
SUBJECT_NOTE = (
    "The subject is the bank line. Linkages and unresolvable ids are both keyed "
    "on bank_line_id, so that is the unit every rate counts. PSP-side "
    "exceptions are diagnostics and sit outside every denominator here; folding "
    "them in would let one bank-line failure be counted twice."
)

#: The one honest limit of the contract, stated once (derivations.ts header).
RATE_ONLY_NOTE = (
    "Metrics carries rates, not the integers they were computed from: there is "
    "no resolvable_subject_count field on the wire. So each denominator is "
    "NAMED above and, apart from the trap denominator and match_count, cannot "
    "be shown as a figure from this run. A derived integer that looks measured "
    "is worse than an absent one, so this report does not divide one wire float "
    "by another to manufacture one."
)

#: The small-denominator caveat, from `web/lib/metric-shape.ts`. Applied at the
#: threshold that module uses.
SMALL_TRAP_DENOMINATOR = 20
SMALL_TRAP_CAVEAT = (
    "A small denominator. This is every deliberately unresolvable subject in "
    "the dataset, not a sample of them - but it is still a handful, and the "
    "larger datasets are the stronger evidence."
)
NO_TRAPS_CAVEAT = (
    "There are no traps in this dataset. Declining a trap that does not exist "
    "is not an achievement: this reads as a full capture by definition rather "
    "than by measurement."
)
TRAP_DENOMINATOR_ABSENT = (
    "The trap denominator is not recorded on this run. Metrics.total_traps was "
    "added after this run may have been stored, and an absent denominator is "
    "not a denominator of zero."
)


# --- the tier ladder --------------------------------------------------------


@dataclass(frozen=True)
class Tier:
    """One rung: the rule it applies, and what it takes to fall through.

    `requires` is a property of the ENGINE - true of every run at every scale,
    because it is the rule the code applies. It is deliberately separated from
    anything measured on one dataset, which this report does not carry at all.
    """

    key: str
    rule: str
    requires: tuple[tuple[str, str], ...] = field(default=())
    declines: str = ""
    zero_means: str = ""


#: The ±1 rupee amount tolerance, in paise, as an integer -- so it is rendered
#: by `core.money.fmt_inr` like every other amount rather than typed out with a
#: rupee sign in a string.
TOLERANCE_PAISE = 100
WINDOW_DAYS = 2

TIERS: dict[str, Tier] = {
    "T0": Tier(
        key="T0",
        rule="Reference hit and exact arithmetic.",
        requires=(
            ("Reference", "A UTR, or a settlement id in the narration"),
            ("Arithmetic", "Reconstructed net equals the bank credit exactly"),
            ("Payment legs", "Any number"),
            ("Tolerance", "0 paise"),
            ("Date window", "None"),
        ),
        declines=(
            "The reference resolves but the sum does not close. T0 writes the "
            "residual into the audit trail and lets the line fall through, "
            "rather than claiming it at confidence 1.00 with a net that is not "
            "the bank credit. A settlement id proves identity; it says nothing "
            "about whether the sum closes."
        ),
        zero_means=(
            "No bank line in this run both named its settlement and closed to "
            "the paise. On a dataset with any clean settlements at all that "
            "would be surprising - this is the rung that carries the bulk of "
            "the work."
        ),
    ),
    "T1": Tier(
        key="T1",
        rule="Reconstruction closes exactly, from exactly one payment leg.",
        requires=(
            ("Reference", "Not required"),
            ("Arithmetic", "Reconstructed net equals the credit exactly"),
            ("Payment legs", "Exactly one"),
            ("Tolerance", "0 paise"),
            ("Date window", f"+/- {WINDOW_DAYS} days"),
        ),
        declines=(
            "More than one unclaimed settlement satisfies the arithmetic and "
            "the window. More than one valid candidate means match nothing, and "
            "the candidate set goes into the audit trail."
        ),
        zero_means=(
            "Nothing in this run lost its reference on a single-payment-leg "
            "settlement. A dataset with no garbled narrations, or one whose "
            "garbled lines all sit on multi-leg batches, reports zero here and "
            "is correct."
        ),
    ),
    "T2": Tier(
        key="T2",
        rule="Reconstruction closes exactly, from two or more payment legs.",
        requires=(
            ("Reference", "Not required"),
            ("Arithmetic", "Reconstructed net equals the credit exactly"),
            ("Payment legs", "Two or more"),
            ("Tolerance", "0 paise"),
            ("Date window", f"+/- {WINDOW_DAYS} days"),
        ),
        declines=(
            "The same ambiguity rule as T1. Candidacy is searched blind to "
            "cardinality and the ambiguity rule is applied to the whole "
            "candidate set afterwards - filtering the pool by leg count first "
            "would turn the T1/T2 split into a tie-breaker, and each settlement "
            "would look unique inside its own partition."
        ),
        zero_means=(
            "No multi-leg batch in this run lost its reference. On a small "
            "dataset this is the ordinary result, not a rung that failed: a "
            "zero here is a measurement."
        ),
    ),
    "T3": Tier(
        key="T3",
        rule="The same reconstruction, accepted within tolerance.",
        requires=(
            ("Reference", "Not required"),
            ("Arithmetic", "Reconstructed net within tolerance of the credit"),
            ("Payment legs", "Any number"),
            ("Tolerance", "{tolerance}, the loosest amount window in the system"),
            ("Date window", f"+/- {WINDOW_DAYS} days"),
        ),
        declines=(
            "The residual is larger than the tolerance, or more than one "
            "candidate closes within it. The delta is recorded in the match "
            "evidence rather than discarded, so a tolerance match never looks "
            "like an exact one."
        ),
        zero_means=(
            "No line in this run needed the tolerance. Every match closed to "
            "the paise or did not close at all."
        ),
    ),
    "LLM": Tier(
        key="LLM",
        rule="The analyst proposed a resolution and the checker accepted it.",
        requires=(
            ("Proposal", "A hypothesis naming one bank line and a set of legs"),
            (
                "Checks",
                "Six checks over five frozen labels, all of which must hold: "
                "existence, exclusivity, causality, arithmetic, coherence and "
                "uniqueness, in that order, returning on the first failure",
            ),
            (
                "Subject tie",
                "The hypothesis's subject must be the bank line it proposes "
                "against - a seventh gate, owned by the accept loop",
            ),
            ("Amount tolerance", "{tolerance} - T3's tolerance, deliberately. A model proposal does not get a wider window than deterministic code would have taken"),
            (
                "Money",
                "Recomputed by the matcher's own reconstruction from the legs, "
                "never taken from the model; the net is the bank credit",
            ),
        ),
        declines=(
            "Any one of the six checks fails, or the subject tie does not hold. "
            "The proposal, the free-text reason and the machine-readable failed "
            "check all survive onto the exception, which is where a rejection "
            "becomes visible rather than being discarded."
        ),
        zero_means=(
            "Either no model was called on this run, or one was called and "
            "nothing it proposed survived the checks. Those are different "
            "results and a zero here alone does not separate them; the analyst "
            "section below is where this report tries."
        ),
    ),
}


# --- what a confidence means ------------------------------------------------

#: Keyed on the figure the ENGINE stamped, formatted to two places. A key that
#: is not present renders a generic sentence rather than a guess -- this table
#: explains figures, it never supplies one.
CONFIDENCE_MEANING: dict[str, str] = {
    "1.00": (
        "The reference resolved and the reconstruction closes to the exact "
        "paise. No model ran on this rung - this is an identity, not a "
        "probability, and there is no residual uncertainty for it to express."
    ),
    "0.99": (
        "The reconstruction closes exactly from two or more legs, with no "
        "reference corroborating it. The withheld point is the chance that a "
        "different leg set closes to the same net."
    ),
    "0.95": (
        "The reconstruction closes exactly from a single leg, inside the date "
        "window, with exactly one candidate."
    ),
    "0.80": (
        "Accepted inside the amount tolerance. The residual is recorded on the "
        "match."
    ),
    "0.70": (
        "Proposed by the analyst and accepted by all six checks. This is the "
        "engine's figure - the model's own self-assessment is never read by any "
        "check."
    ),
}

CONFIDENCE_GENERIC = "Stamped by the engine on every match from this rung on this run."
CONFIDENCE_NO_MATCHES = (
    "No match from this rung on this run, so there is nothing to report. That "
    "is a result, not a gap."
)
CONFIDENCE_CONFLICT = (
    "This rung stamped more than one confidence on this run. A single figure "
    "would be true of some of its matches and false of the rest, so none is "
    "shown."
)
CONFIDENCE_ABSENT = (
    "RunSummary.tier_confidence is not reported on this run. It was added after "
    "this run may have been stored, and a run that predates the field carries "
    "no stamp to read. A plausible-looking constant printed here is precisely "
    "the defect this field exists to prevent: the console's hardcoded table "
    "rendered a reassuring word on the rung the engine stamps 0.70, and nothing "
    "bound the two together, so nothing caught the drift."
)

TIER_LADDER_NOTE = (
    "The counting unit here is the MATCH GROUP, from the engine's own tier "
    "assignment - deliberately not the subject counts the four rates above "
    "report, which is why each names its own denominator. Five rungs run in "
    "order and a subject leaves the pool the moment an earlier rung claims it. "
    "All five rows are always printed, zeros included: a rung that matched "
    "nothing is a result, and 'we do not know what this rung did' is a "
    "different claim."
)


# --- the standing limits ----------------------------------------------------

STANDING_LIMITS_HEADING = "What this run cannot tell you"
STANDING_LIMITS_LEDE = (
    "Limits of the run as a whole rather than of one number. Each is "
    "conditioned on what this run actually reports; none of it is standing "
    "boilerplate."
)

#: Carried verbatim in substance from `StandingLimits` in
#: `web/app/runs/[id]/page.tsx`. These three are unconditional on a SCORED run,
#: exactly as they are there -- that component renders only when metrics exist.
SCORED_LIMITS: tuple[tuple[str, str], ...] = (
    (
        "The gap to 100% is one defect class, and it is not a long tail",
        "Every unmatched resolvable subject is a split settlement: one "
        "settlement paid across two bank lines, which closes neither line on "
        "its own. Both narrations name the settlement, so the reference lookup "
        "hits and then declines - a settlement id proves identity, never "
        "arithmetic. Solving it needs a new tier that searches subsets of bank "
        "lines, and a subset search is exactly where a tie-breaker on an "
        "ambiguous set creeps back in. It was left unimplemented on purpose.",
    ),
    (
        "Five of the eight reason codes are not exercised by this generator",
        "They are implemented and unit-tested, and this data does not produce "
        "them. A zero beside one of those codes on the exception list means not "
        "exercised here, not handled.",
    ),
    (
        "The scorecard is demonstrably blind to at least one real bug",
        "A cardinality-filtered candidate pool - the precise defect the "
        "ambiguity trap exists to catch - leaves every metric at every scale "
        "byte-identical. The unit tests catch it; nothing on the scorecard "
        "does.",
    ),
)

#: Conditional, and the condition is `llm_tokens_per_100 == 0` and
#: `llm_cost_usd_per_100 == 0`. The claim it replaces was an unconditional
#: "no model was called on this run", which is the inference `web/lib/analyst.ts`
#: documents at length as invalid.
LLM_UNBILLED_LIMIT = (
    "This report cannot tell you whether a model was called",
    "llm_tokens_per_100 and llm_cost_usd_per_100 are both zero, and zero is "
    "not proof of absence: a run whose analyst was switched off, a model that "
    "was called and proposed nothing, and a model that was called and whose "
    "provider failed all bill exactly this. CreateRunRequest.use_llm is a "
    "request field and RunSummary does not echo it back, so no typed field on "
    "this contract separates them. Read every LLM figure in this report as not "
    "established rather than as a measured zero.",
)

METHOD_POINTER = (
    "The method page of the console carries the pipeline, the tier ladder, the "
    "checker's six checks and the full list of known limitations."
)


# --- the analyst's states ---------------------------------------------------

#: `web/lib/analyst.ts`. The report reaches a firmer verdict than the console
#: does, and for one reason: it is handed the WHOLE exception list, so the
#: hypothesis census is complete rather than partial, and "no evidence" becomes
#: decidable instead of "still reading".
ANALYST_TITLE: dict[str, str] = {
    "accepted": "The analyst ran, and the checker accepted some of what it proposed",
    "all-rejected": "The analyst ran and proposed, and every hypothesis was refused",
    "called-silently": "A model was called, and it proposed nothing",
    "no-evidence": "Nothing on the wire says a model was involved in this run",
}

ANALYST_EXPLANATION: dict[str, str] = {
    "accepted": (
        "Hypotheses reached the assisted tier, which means each of them "
        "satisfied existence, exclusivity, causality, arithmetic, coherence and "
        "uniqueness, and was tied to the bank line it named. None of the money "
        "on those matches came from the model: every amount was recomputed by "
        "the matcher's own reconstruction from the legs, and the net is the "
        "bank credit. Read this beside the rejection rate - a checker that "
        "accepts everything scores a rejection rate of zero and would look "
        "identical here on the accept side alone."
    ),
    "all-rejected": (
        "The model proposed and the checker refused all of it, so the rejection "
        "rate is 1.0 and nothing reached the assisted tier. That is a result, "
        "not a failure, and on the seeded datasets it is the CORRECT result: "
        "the residue the deterministic rungs leave splits into subjects that "
        "two settlements close identically - which no engine may resolve - and "
        "split settlements, which no single-settlement hypothesis can express "
        "at all. A run that accepted something out of that residue would be the "
        "thing to investigate."
    ),
    "called-silently": (
        "Tokens were billed, so a model was called, and it put forward no "
        "hypothesis at all. That is a model correctly declining to guess about "
        "a residue with no answer in it, and it is a different result from a "
        "checker that refused what was proposed - nothing reached the checker "
        "to refuse."
    ),
    "no-evidence": (
        "No tokens, no cost, no accepted hypothesis, no rejection, and no "
        "proposal anywhere on the exception list this report was handed. "
        "Nothing here is evidence that a model ran - and that is a weaker "
        "statement than 'no model ran', which these fields cannot support. "
        "THREE DIFFERENT RUNS PRODUCE EXACTLY THESE VALUES: one with the "
        "analyst switched off, one where use_llm was requested with no "
        "credential configured, and one where the call was made and the "
        "provider refused it, after which the run completes on its "
        "deterministic result and bills nothing. use_llm is a field on the "
        "REQUEST and RunSummary does not echo it back, so no typed field on "
        "this contract separates the three."
    ),
}


# --- an unscored run --------------------------------------------------------

#: `NoMetricsYet` in `web/app/runs/[id]/page.tsx`, the uploaded-files branch.
UNSCORED_HEADING = "This run is unscored, and it always will be"
UNSCORED_BODY = (
    "It reconciled files that were uploaded. Every rate on a scorecard is "
    "measured against ground truth - the generator's own record of which order "
    "settled into which batch and landed on which bank line - and no such "
    "record exists for a merchant's own exports. So RunSummary.metrics is null, "
    "this report prints no match rate, and it does not print a zero in its "
    "place: a rate computed against the run's own output would be a number "
    "grading itself."
)
UNSCORED_WHAT_IT_DOES_TELL_YOU = (
    "Everything except how well it did. The settlements it reconstructed, the "
    "bank credit each one closed against, and every subject it could not close "
    "with the reason for each, are findings about these books and they do not "
    "depend on anyone knowing the answer in advance. The measured accuracy of "
    "this engine is published against the generated datasets, where ground "
    "truth exists and the number means something."
)

FAILED_HEADING = "This run failed before it produced metrics"
FAILED_BODY = (
    "The run reached a terminal state without a scorecard, so there is nothing "
    "here to read - not a zero, an absence."
)

RUNNING_HEADING = "This run has not finished, so it is unscored so far"
RUNNING_BODY = (
    "RunSummary.metrics is null until the run reaches a terminal state, and "
    "this report will not print a zero in the meantime: a measured zero and an "
    "absent number are different claims."
)


# --- exceptions -------------------------------------------------------------

REASON_CODE_DESCRIPTION: dict[str, str] = {
    "NO_SETTLEMENT_REF": (
        "The narration carries no settlement id, so there is no reference to "
        "match on."
    ),
    "AMOUNT_MISMATCH": (
        "A candidate settlement was found but its reconstructed net does not "
        "equal the bank credit."
    ),
    "ORPHAN_BANK_LINE": (
        "A bank line with no settlement batch that reconstructs to it."
    ),
    "ORPHAN_PSP_TXN": "A PSP leg belonging to no settlement that reached the bank.",
    "DUPLICATE_PSP_TXN": (
        "The same order-bearing leg appears twice. Fee and tax legs repeat once "
        "per settlement by design and are not duplicates."
    ),
    "AMBIGUOUS_MULTI_CANDIDATE": (
        "More than one candidate settlement satisfies the arithmetic. This "
        "subject is unresolvable by construction and declining it is the "
        "correct outcome."
    ),
    "UNPARSEABLE_NARRATION": (
        "The narration yielded no settlement id, no UTR and no entity."
    ),
    "MISSING_ORDER_REF": (
        "A PSP leg carries no order_id, so the order behind it must be "
        "recovered from the batch."
    ),
}

SUBJECT_TYPE_LABEL: dict[str, str] = {
    "order": "Order",
    "psp_txn": "PSP transaction",
    "bank_line": "Bank line",
}

EXCEPTIONS_NOTE = (
    "Every subject this run could not close, with the reason code the engine "
    "assigned. AMBIGUOUS_MULTI_CANDIDATE is not a failure: it is the designed "
    "outcome for a subject the data does not determine, and it is what the trap "
    "capture rate measures."
)

EXCEPTIONS_EMPTY = (
    "No exception rows were supplied for this run. The run reports "
    "exception_count as the figure in the identity section above; if that "
    "figure is not zero, this report was handed an empty list rather than "
    "shown a run that produced none."
)


# --- quarantine -------------------------------------------------------------

QUARANTINE_NOTE = (
    "Rows an upload could not turn into a record, kept verbatim. A quarantined "
    "row is merchant data and reaches a reader only through an authenticated "
    "read; it never appears in an error body. An adapter that accepts a file "
    "and produces no records must say why here - records and quarantine both "
    "empty is never a valid outcome."
)

QUARANTINE_ABSENT = (
    "No quarantine rows were supplied to this report. That is a statement about "
    "this report's inputs and not a claim that nothing was quarantined: the "
    "argument was not supplied, so no table is printed in its place."
)


# --- provenance -------------------------------------------------------------

PROVENANCE_SEEDED = (
    "A generated dataset. The seed and the record count below are what "
    "reproduce it; RunSummary carries no dataset id, so this report does not "
    "print one."
)

PROVENANCE_UPLOADS = (
    "Files that were uploaded. The content hash is the identity of each one and "
    "the upload id is only a name for it, so re-uploading the same file returns "
    "the row that already exists rather than a second one."
)

PROVENANCE_UPLOADS_ABSENT = (
    "The run's seed marks it as a run over uploaded files, but no upload rows "
    "were supplied to this report, so the files it read are not named here."
)

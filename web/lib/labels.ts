/**
 * Display copy for the contract's typed enums.
 *
 * Every label here is keyed off a wire enum value -- `ReasonCode`,
 * `VerifierCheck`, `RunState` -- never off free text. `verifier_reason` is
 * prose for a human and is rendered verbatim; it is never parsed to work out
 * which check failed (LANE-E-web.md §7.2).
 */
import type { ReasonCode, RunState, VerifierCheck } from "./types";

export const REASON_CODES: readonly ReasonCode[] = [
  "NO_SETTLEMENT_REF",
  "AMOUNT_MISMATCH",
  "ORPHAN_BANK_LINE",
  "ORPHAN_PSP_TXN",
  "DUPLICATE_PSP_TXN",
  "AMBIGUOUS_MULTI_CANDIDATE",
  "UNPARSEABLE_NARRATION",
  "MISSING_ORDER_REF",
] as const;

export const REASON_CODE_LABEL: Record<ReasonCode, string> = {
  NO_SETTLEMENT_REF: "No settlement reference",
  AMOUNT_MISMATCH: "Amount mismatch",
  ORPHAN_BANK_LINE: "Orphan bank line",
  ORPHAN_PSP_TXN: "Orphan PSP transaction",
  DUPLICATE_PSP_TXN: "Duplicate PSP transaction",
  AMBIGUOUS_MULTI_CANDIDATE: "Ambiguous — multiple candidates",
  UNPARSEABLE_NARRATION: "Unparseable narration",
  MISSING_ORDER_REF: "Missing order reference",
};

export const REASON_CODE_DESCRIPTION: Record<ReasonCode, string> = {
  NO_SETTLEMENT_REF:
    "The narration carries no settlement id, so there is no reference to match on.",
  AMOUNT_MISMATCH:
    "A candidate settlement was found but its reconstructed net does not equal the bank credit.",
  ORPHAN_BANK_LINE: "A bank line with no settlement batch that reconstructs to it.",
  ORPHAN_PSP_TXN: "A PSP leg belonging to no settlement that reached the bank.",
  DUPLICATE_PSP_TXN:
    "The same order-bearing leg appears twice. Fee and tax legs repeat once per settlement by design and are not duplicates.",
  AMBIGUOUS_MULTI_CANDIDATE:
    "More than one candidate settlement satisfies the arithmetic. This subject is unresolvable by construction and declining it is the correct outcome.",
  UNPARSEABLE_NARRATION:
    "The narration yielded no settlement id, no UTR and no entity.",
  MISSING_ORDER_REF:
    "A PSP leg carries no order_id, so the order behind it must be recovered from the batch.",
};

/**
 * Reason codes where leaving the subject unmatched is the *designed* outcome,
 * not a failure. These are never styled as errors and never get a
 * "resolve anyway" affordance (LANE-E-web.md §9.4).
 */
export const UNRESOLVABLE_BY_DESIGN: ReadonlySet<ReasonCode> = new Set([
  "AMBIGUOUS_MULTI_CANDIDATE",
]);

export const VERIFIER_CHECKS: readonly VerifierCheck[] = [
  "existence",
  "exclusivity",
  "causality",
  "arithmetic",
  "uniqueness",
] as const;

export const VERIFIER_CHECK_LABEL: Record<VerifierCheck, string> = {
  existence: "Existence",
  exclusivity: "Exclusivity",
  causality: "Causality",
  arithmetic: "Arithmetic",
  uniqueness: "Uniqueness",
};

/**
 * What each check actually asserts, in the order the verifier runs them. It
 * returns on the first failure, so a rejection names the earliest rule the
 * hypothesis broke and not every rule it broke.
 *
 * `uniqueness` is the one worth spelling out: it means the model proposed a
 * resolution and the verifier refused it as ambiguous, which is the same
 * ambiguity rule the deterministic tiers obey. An LLM is not permitted to
 * resolve what deterministic code correctly refused.
 */
export const VERIFIER_CHECK_DESCRIPTION: Record<VerifierCheck, string> = {
  existence:
    "Every id the hypothesis names exists, the bank line carries a positive credit, the proposal is not empty, and no transaction id is named twice.",
  exclusivity:
    "None of the legs it claims may already belong to a match that was accepted earlier. One PSP leg funds one bank line.",
  causality:
    "Every leg must have settled, and must have settled on or before the bank line's own date. Money cannot arrive in the bank before the settlement that produced it, and a leg that never settled cannot have funded a credit at all.",
  arithmetic:
    "The reconstructed net of the proposed legs must equal the bank credit within ₹1.00 — the loosest deterministic tier's tolerance, and no wider. It is recomputed by the matcher's own reconstruction rather than taken from the model.",
  uniqueness:
    "More than one unclaimed settlement closes the same bank line within the same tolerance, so the verifier refused to pick between them.",
};

/**
 * THE FIVE LABELS ARE NOT FIVE CHECKS.
 *
 * The verifier runs six branches over five frozen spellings, and the accept
 * loop adds a seventh gate outside the verifier entirely. All three of the
 * extras report under `existence`, because `VerifierCheck` is set-identical to
 * `ReconException.failed_check` on the frozen contract and a sixth spelling
 * would be a contract change.
 *
 * The consequence for any count aggregated over `failed_check`: the `existence`
 * bucket holds three populations and therefore UNDERSTATES the guardrail. It is
 * a floor, not a decomposition. Splitting it would mean parsing
 * `verifier_reason`, which is prose for a human — so the UI names the
 * conflation and shows the reason text verbatim instead of guessing.
 */
export const VERIFIER_CHECK_CONFLATION: Partial<Record<VerifierCheck, string>> = {
  existence:
    "This label carries three different rules: existence proper, the coherence check — the proposal must BE one settlement, all of its legs and no others, which is the most interesting thing this verifier does — and the accept loop's subject tie, which refuses a hypothesis whose subject is not the bank line it proposes against. A count under this label is a floor on how often the guardrail fired, not a breakdown of which rule fired.",
};

export const RUN_STATE_LABEL: Record<RunState, string> = {
  pending: "Pending",
  running: "Running",
  completed: "Completed",
  failed: "Failed",
};

export const TERMINAL_RUN_STATES: ReadonlySet<RunState> = new Set([
  "completed",
  "failed",
]);

export function isTerminal(state: RunState): boolean {
  return TERMINAL_RUN_STATES.has(state);
}

/**
 * `RunSummary.seed` for a run whose inputs a merchant uploaded.
 *
 * There is no seed: nothing generated those records. `RunSummary` is frozen
 * and `seed` is a non-nullable integer on it, so the API answers with a value
 * no caller could have supplied -- the generator refuses negatives -- rather
 * than with a `0` that reads as a real experiment.
 *
 * Everywhere this console would print a seed, it checks this first and says
 * what actually happened instead.
 */
export const UNSEEDED_RUN = -1;

/** True when this run reconciled uploaded files rather than a generated dataset. */
export function isFromUploads(run: { seed: number }): boolean {
  return run.seed === UNSEEDED_RUN;
}

export const SUBJECT_TYPE_LABEL: Record<
  "order" | "psp_txn" | "bank_line",
  string
> = {
  order: "Order",
  psp_txn: "PSP transaction",
  bank_line: "Bank line",
};

export const VERDICT_LABEL: Record<
  "accepted" | "rejected" | "not_attempted",
  string
> = {
  accepted: "Accepted",
  rejected: "Rejected",
  not_attempted: "Not attempted",
};

export const AUDIT_ACTOR_LABEL: Record<
  "deterministic" | "llm" | "verifier",
  string
> = {
  deterministic: "Deterministic",
  llm: "LLM",
  verifier: "Verifier",
};

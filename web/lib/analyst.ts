/**
 * THREE STATES THAT LOOK IDENTICAL IF YOU ARE CARELESS.
 *
 * On the analyst view, all three of these render as zeros:
 *
 *   1. No model was ever called.
 *   2. A model ran and correctly proposed nothing.
 *   3. A model proposed, and the verifier rejected everything.
 *
 * They are different results. The third is the guardrail firing and is the
 * strongest single fact this system can report about its own honesty; the
 * second is a model with nothing to say about a residue that genuinely has no
 * answer; the first is a switch that was off. A view that showed "0 accepted"
 * for all three would be telling the reader nothing while looking like it had
 * told them something.
 *
 * This module is the discriminator, and it is written to be read: each state
 * names the wire facts that put it there, and where the wire cannot tell two
 * states apart it says so instead of choosing.
 *
 * WHAT THE WIRE DOES AND DOES NOT CARRY.
 *
 *   - `Metrics.tier_counts.LLM` — hypotheses that survived every check and
 *     became matches. A positive value is proof a model ran.
 *   - `Metrics.llm_rejection_rate` — rejected over proposed. Positive is proof
 *     a model ran AND proposed. Zero is ambiguous: nothing proposed and
 *     nothing rejected produce the same 0.0 as an empty denominator.
 *   - `Metrics.llm_tokens_per_100` / `llm_cost_usd_per_100` — what was billed.
 *     Positive is proof of a call. Zero is NOT proof of no call: the
 *     project's own stubbed run proposed ten hypotheses and billed nothing.
 *   - `ReconException.llm_hypothesis` — a proposal, on a subject that is still
 *     an exception. Counted by scanning the exception list.
 *
 *   - `CreateRunRequest.use_llm` — the flag that actually decides, and it is a
 *     REQUEST field. `RunSummary` does not echo it. So a run with the analyst
 *     switched off and a run whose analyst was called, proposed nothing and
 *     billed nothing are indistinguishable on this contract, and the copy for
 *     that state says so rather than guessing.
 *
 *   - `RunStatus.stage` carries a free-text summary that on the live API reads
 *     like "complete (LLM: 10 proposed, 0 accepted, 10 rejected)". It is
 *     documented as free text. Parsing it would be reading numbers out of
 *     prose, which is the same mistake as recovering `failed_check` from
 *     `verifier_reason`, so THIS MODULE does not look at it, and no state
 *     below is decided by it.
 *
 *     THAT RULE IS ABOUT COUNTS, AND IT DOES NOT EXTEND TO DIAGNOSTICS. On the
 *     live API `stage` is the ONLY place the contract carries the difference
 *     between an analyst that was switched off, one that had no credential
 *     ("use_llm was requested, but no analyst credential is set: set
 *     ANTHROPIC_API_KEY or GEMINI_API_KEY…") and one whose provider failed
 *     ("the LLM pass failed (GeminiCallFailed: … 503 UNAVAILABLE …): the
 *     deterministic result stands and LLM metrics are zero"). None of those is
 *     a count. Three runs that differ only in which of them happened produce
 *     identical values in every field this module reads, so a view that never
 *     showed `stage` could not tell a misconfigured deployment from a
 *     deliberately deterministic run — and never once mentioned a missing key.
 *
 *     So `components/analyst/RunStageAccount.tsx` renders that string VERBATIM,
 *     beside this verdict and never folded into it, under the same rule the
 *     exception list applies to `verifier_reason`: show the prose, do not parse
 *     it. The states below stay decided by typed fields alone.
 */
import type { HypothesisCensus } from "./census";
import type { Metrics } from "./types";

export type AnalystState =
  /** Positive proof: something the model proposed became a match. */
  | "accepted"
  /** Positive proof it proposed, and nothing survived the verifier. */
  | "all-rejected"
  /** Positive proof it was called and billed, with nothing proposed. */
  | "called-silently"
  /** No evidence on the wire that any model was involved. */
  | "no-evidence"
  /** Not enough of the exception list has been read to say yet. */
  | "undetermined";

export type AnalystEvidence = {
  label: string;
  value: string;
  /** What this fact alone rules in or out. */
  implication: string;
};

export type AnalystVerdict = {
  state: AnalystState;
  /** The heading for this state. A statement, not a status word. */
  title: string;
  evidence: AnalystEvidence[];
};

const int = (n: number) => n.toLocaleString("en-IN");
const rate = (n: number) => `${(n * 100).toFixed(1)}%`;

export function analystVerdict(
  metrics: Metrics,
  census: HypothesisCensus | null,
): AnalystVerdict {
  const accepted = metrics.tier_counts.LLM;
  const billed =
    metrics.llm_tokens_per_100 > 0 || metrics.llm_cost_usd_per_100 > 0;
  const proposalsSeen = census?.withHypothesis ?? 0;
  const rejectionRate = metrics.llm_rejection_rate;

  const evidence: AnalystEvidence[] = [
    {
      label: "tier_counts.LLM",
      value: int(accepted),
      implication:
        accepted > 0
          ? "Hypotheses that passed every check and became matches. A positive value here is proof a model ran."
          : "No hypothesis became a match. On its own this rules nothing out — it is equally true of a run with no model, a model with nothing to say, and a verifier that refused everything.",
    },
    {
      label: "llm_rejection_rate",
      value: rate(rejectionRate),
      implication:
        rejectionRate > 0
          ? "Rejected over proposed. A positive rate is proof a model both ran and proposed something."
          : "Rejected over proposed. Zero here means nothing was rejected — which is what an empty denominator also reports, so it does not distinguish 'nothing proposed' from 'nothing refused'.",
    },
    {
      label: "llm_tokens_per_100",
      value: int(metrics.llm_tokens_per_100),
      implication: billed
        ? "Tokens were billed, so a model was called on this run."
        : "Nothing was billed. That is not proof of no call: the project's own stubbed run proposed ten hypotheses through the full pipeline and billed nothing, because the client was stubbed rather than live.",
    },
    {
      label: "llm_cost_usd_per_100",
      value: `$${metrics.llm_cost_usd_per_100.toFixed(4)}`,
      implication:
        "US dollars per 100 records. The one money figure in this product that is not integer paise, and it moves with the token count rather than independently of it.",
    },
    {
      label: "hypotheses on the exception list",
      value:
        census === null
          ? "not counted"
          : census.complete
            ? int(proposalsSeen)
            : `${int(proposalsSeen)} so far, of ${int(census.scanned)} rows read`,
      implication:
        "Counted by reading the exception list rather than derived from a rate. It cannot see an ACCEPTED hypothesis: acceptance makes the subject a match, and a match is not an exception.",
    },
  ];

  if (accepted > 0) {
    return {
      state: "accepted",
      title: "The analyst ran, and the verifier accepted some of what it proposed",
      evidence,
    };
  }

  if (proposalsSeen > 0 || rejectionRate > 0) {
    return {
      state: "all-rejected",
      title: "The analyst ran and proposed, and every hypothesis was refused",
      evidence,
    };
  }

  if (billed) {
    return {
      state: "called-silently",
      title: "A model was called, and it proposed nothing",
      evidence,
    };
  }

  if (census !== null && census.complete) {
    return {
      state: "no-evidence",
      title: "Nothing on the wire says a model was involved in this run",
      evidence,
    };
  }

  return {
    state: "undetermined",
    title: "Still reading the exception list",
    evidence,
  };
}

/**
 * What each state means, at length, including what it is NOT. The copy is here
 * rather than in the component because the distinction between these five is
 * the entire content of the view — the layout is just where it is put.
 */
export const ANALYST_STATE_EXPLANATION: Record<AnalystState, string> = {
  accepted:
    "Hypotheses reached the assisted tier, which means each of them satisfied existence, exclusivity, causality, arithmetic, coherence and uniqueness, and was tied to the bank line it named. None of the money on those matches came from the model: every amount was recomputed by the matcher's own reconstruction from the legs, and the net is the bank credit. Read this beside the rejection rate — a verifier that accepts everything scores a rejection rate of zero, and would look identical here on the accept side alone.",
  "all-rejected":
    "The model proposed and the verifier refused all of it, so the rejection rate is 1.0 and nothing reached the assisted tier. That is a result, not a failure, and on the seeded datasets it is the CORRECT result: the residue the deterministic tiers leave splits into subjects that two settlements close identically — which no engine may resolve, and resolving one would drop the trap-capture rate below 1.0 — and split settlements, which no single-settlement hypothesis can express at all. A run that accepted something out of that residue would be the thing to investigate. Read it beside the assisted match rate: a verifier that rejects everything unconditionally also scores 1.0 here.",
  "called-silently":
    "Tokens were billed, so a model was called, and it put forward no hypothesis at all. That is a model correctly declining to guess about a residue with no answer in it, and it is a different result from a verifier that refused what was proposed — nothing reached the verifier to refuse.",
  "no-evidence":
    "No tokens, no cost, no accepted hypothesis, no rejection, and no proposal anywhere on the exception list. Nothing here is evidence that a model ran — and that is a weaker statement than 'no model ran', which these fields cannot support. THREE DIFFERENT RUNS PRODUCE EXACTLY THESE VALUES: one with the analyst switched off, one where use_llm was requested with no credential configured, and one where the call was made and the provider refused it, after which the run completes on its deterministic result and bills nothing. use_llm is a field on the REQUEST and RunSummary does not echo it back, so no typed field on this contract separates the three. The run's own status does, in prose, and it is reproduced verbatim below rather than guessed at here. Until you have read it, treat every LLM figure on this page as not established rather than as a measured zero.",
  undetermined:
    "The exception list is still being read. Until it has been read to the end, an absence of proposals is an absence of evidence rather than evidence of absence, and this view will not claim otherwise.",
};

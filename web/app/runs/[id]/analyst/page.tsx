"use client";

import Link from "next/link";
import { useHypothesisCensus } from "@/lib/census";
import { type MetricKey } from "@/lib/derivations";
import { isTerminal } from "@/lib/labels";
import { useRun } from "@/components/shell/RunScope";
import { ViewIntro } from "@/components/shell/ViewIntro";
import { EmptyState, ErrorState, LoadingBlock } from "@/components/States";
import { MetricDisclosure } from "@/components/metrics/MetricDisclosure";
import { AnalystVerdict } from "@/components/analyst/AnalystVerdict";
import { RunStageAccount } from "@/components/analyst/RunStageAccount";
import {
  Coverage,
  HypothesisAccounting,
  RejectionBreakdown,
} from "@/components/analyst/Hypotheses";

/**
 * The LLM layer, honestly.
 *
 * The analyst proposes and deterministic code disposes. Nothing the model says
 * becomes a number: every money field on an accepted match is recomputed by the
 * matcher's own reconstruction from the legs, the net is the bank credit, and
 * the hypothesis's own confidence is recorded in evidence and read by no check.
 *
 * THE WHOLE POINT OF THIS VIEW IS TO SEPARATE THREE OUTCOMES THAT ALL RENDER AS
 * ZERO: a model that was never called, a model that ran and correctly proposed
 * nothing, and a model that proposed and had everything refused. The third is
 * the strongest thing this system can say about itself and the first is a
 * switch that was off, and a page that showed "0 accepted" for both would have
 * said nothing while looking like it had said something. `lib/analyst.ts` is
 * the discriminator and it names the wire fact behind every branch.
 *
 * On the seeded datasets the honest answer is that the analyst proposes nothing
 * acceptable, because the residue the deterministic tiers leave is genuinely
 * unresolvable: ambiguity traps that no engine may resolve, and split
 * settlements that no single-settlement hypothesis can express. This page
 * explains that rather than apologising for it.
 */
const COST_ROWS: readonly MetricKey[] = [
  "llm_rejection_rate",
  "assisted_match_rate",
  "llm_tokens_per_100",
  "llm_cost_usd_per_100",
];

export default function AnalystPage() {
  const run = useRun();
  const metrics = run.metrics;
  const terminal = isTerminal(run.state);

  const { data: census, error, loading, scanning, refresh } =
    useHypothesisCensus(run.run_id, run.state, terminal && metrics !== null);

  return (
    <div className="space-y-14">
      <ViewIntro
        title="Analyst and verifier"
        lede="The analyst only ever sees the residue the deterministic tiers declined, and everything it proposes is put through six checks over five frozen labels — all of which must hold — plus a seventh gate owned by the accept loop. A rejection is the guardrail firing and is reported as a result, not swept up as a failure."
      />

      {metrics === null ? (
        <EmptyState
          title={
            run.state === "failed"
              ? "This run failed before it produced a scorecard"
              : "No analyst figures yet"
          }
          reason={
            run.state === "failed" ? (
              <>
                There is no <code className="font-mono">metrics</code> object to
                read, so there are no token, cost or rejection figures — an
                absence rather than a set of zeros. Those are different claims
                and this page will not print the second in place of the first.
              </>
            ) : (
              <>
                The analyst runs after the deterministic tiers have produced a
                residue, and <code className="font-mono">RunSummary.metrics</code>{" "}
                is null until the run finishes. This view updates on its own —
                the summary is polled every 500&nbsp;ms until the run reaches a
                terminal state.
              </>
            )
          }
        />
      ) : error ? (
        <ErrorState
          title="The hypothesis count did not load"
          error={error}
          recovery={
            <>
              Hypotheses and verdicts ride on exception rows, and this view reads
              them from{" "}
              <code className="font-mono">
                GET /api/runs/{run.run_id}/exceptions
              </code>
              , page by page. That request failed partway through, so there is no
              count rather than a partial one presented as a total. Retry below;
              the figures from{" "}
              <code className="font-mono">metrics</code> are unaffected and the
              exception list itself may still load.
            </>
          }
          onRetry={refresh}
        />
      ) : loading ? (
        <LoadingBlock
          label="Reading the exception list for hypotheses and verdicts"
          lines={4}
          className="max-w-xl"
        />
      ) : census === null ? null : (
        <>
          <AnalystVerdict metrics={metrics} census={census} />

          <RunStageAccount runId={run.run_id} runState={run.state} />

          <section aria-labelledby="accounting-heading" className="space-y-6">
            <div className="space-y-2">
              <h2
                id="accounting-heading"
                className="text-base font-medium tracking-tight"
              >
                Hypotheses proposed, accepted and rejected
              </h2>
              <Coverage census={census} scanning={scanning} />
            </div>

            <HypothesisAccounting metrics={metrics} census={census} />
          </section>

          <section aria-labelledby="checks-heading" className="space-y-6">
            <div className="space-y-2">
              <h2
                id="checks-heading"
                className="text-base font-medium tracking-tight"
              >
                Which check refused each hypothesis
              </h2>
              <p className="max-w-[72ch] text-xs leading-relaxed text-muted-foreground">
                Read off{" "}
                <code className="font-mono">ReconException.failed_check</code>, a
                typed field, and never recovered from{" "}
                <code className="font-mono">verifier_reason</code>, which is
                prose for a human. All five labels render including the zeros: a
                check that refused nothing is a result, and an omitted row would
                be a silence.
              </p>
            </div>

            <RejectionBreakdown census={census} />
          </section>

          <section aria-labelledby="cost-heading" className="space-y-6">
            <div className="space-y-2">
              <h2
                id="cost-heading"
                className="text-base font-medium tracking-tight"
              >
                Tokens, cost and what they are worth beside each other
              </h2>
              <p className="max-w-[72ch] text-xs leading-relaxed text-muted-foreground">
                Open any row for its definition, its numerator over its
                denominator, and what it does not prove. The rejection rate and
                the assisted match rate are the pair that matter: a verifier that
                refuses everything scores 1.0 on the first, a verifier that
                accepts everything scores 0.0, and neither number is a result on
                its own.
              </p>
            </div>

            <MetricDisclosure
              id="analyst"
              rows={COST_ROWS}
              run={run}
              metrics={metrics}
              caption="Analyst metrics, each expandable to its derivation"
            />
          </section>

          <section aria-labelledby="residue-heading" className="space-y-6">
            <div className="space-y-2">
              <h2
                id="residue-heading"
                className="text-base font-medium tracking-tight"
              >
                Why nothing acceptable is the expected answer here
              </h2>
              <p className="max-w-[72ch] text-xs leading-relaxed text-muted-foreground">
                Stated once, because it is the fact that makes an empty assisted
                tier readable as a result rather than as a disappointment.
              </p>
            </div>

            <ul className="max-w-[72ch] space-y-4">
              <Point title="The ambiguity traps have two answers, so they have none">
                Each of these subjects is closed by exactly two unclaimed
                settlements with an identical net on an identical date. The
                deterministic tiers decline them, and{" "}
                <code className="font-mono">uniqueness</code> declines any
                hypothesis about them for the same reason. An LLM must not be
                permitted to resolve what deterministic code correctly refused —
                and resolving one would drop the trap-capture rate below 1.0,
                which is exactly what that metric exists to catch.
              </Point>

              <Point title="The split settlements cannot even be expressed as a hypothesis">
                One settlement paid across two bank lines closes neither line on
                its own, and{" "}
                <code className="font-mono">coherence</code> requires a proposal
                to BE one settlement: one settlement id, all of its legs and no
                others. There is no set of legs that answers a split half, so no
                model can propose one. The LLM path does not close this gap
                either, and it was never going to.
              </Point>

              <Point title="A rejection is inspectable, which is the whole reason it survives">
                The proposal, the free-text reason and the typed{" "}
                <code className="font-mono">failed_check</code> all persist onto
                the exception, so every refusal on this page can be opened and
                read line by line on the{" "}
                <Link
                  href={`/runs/${encodeURIComponent(run.run_id)}/exceptions`}
                  className="rounded-sm underline underline-offset-4 transition-colors duration-150 hover:text-foreground focus-visible:focus-ring"
                >
                  exception list
                </Link>
                . A guardrail whose firing left no trace would be indistinguishable
                from one that never fired.
              </Point>
            </ul>
          </section>
        </>
      )}
    </div>
  );
}

function Point({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  return (
    <li className="border-l border-border pl-4">
      <p className="text-xs font-medium">{title}</p>
      <p className="mt-1.5 text-xs leading-relaxed text-muted-foreground">
        {children}
      </p>
    </li>
  );
}

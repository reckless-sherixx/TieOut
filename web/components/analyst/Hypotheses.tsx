"use client";

import type { HypothesisCensus } from "@/lib/census";
import {
  VERIFIER_CHECKS,
  VERIFIER_CHECK_CONFLATION,
  VERIFIER_CHECK_DESCRIPTION,
  VERIFIER_CHECK_LABEL,
} from "@/lib/labels";
import { cn } from "@/lib/utils";
import type { Metrics } from "@/lib/types";

const int = (n: number) => n.toLocaleString("en-IN");

/**
 * How far through the exception list the count got.
 *
 * Every number on this view is a count over rows that were actually read, and a
 * count over an unknown fraction of the rows is not a measurement. So the
 * coverage is stated beside the counts rather than under them, and while the
 * scan is running the numbers are explicitly labelled as partial.
 */
export function Coverage({
  census,
  scanning,
}: {
  census: HypothesisCensus;
  scanning: boolean;
}) {
  if (census.complete) {
    return (
      <p className="text-xs text-muted-foreground" role="status">
        Counted over all{" "}
        <span className="tnum">{int(census.total)}</span>{" "}
        {census.total === 1 ? "exception" : "exceptions"} in this run. Complete —
        not a sample.
      </p>
    );
  }
  if (census.truncated) {
    return (
      <p className="max-w-[72ch] text-xs leading-relaxed text-muted-foreground">
        Counted over the first{" "}
        <span className="tnum">{int(census.scanned)}</span> of{" "}
        <span className="tnum">{int(census.total)}</span> exceptions. The scan
        stops at its request budget rather than issuing an unbounded number of
        requests against an endpoint with no aggregate operation, so these are a
        prefix of the list and not a census. Ordering is stable, so the prefix is
        the same one every time.
      </p>
    );
  }
  return (
    <p className="text-xs text-muted-foreground" role="status">
      Counting: <span className="tnum">{int(census.scanned)}</span> of{" "}
      <span className="tnum">{int(census.total)}</span> exceptions read
      {scanning ? "…" : "."} These figures are partial until it finishes.
    </p>
  );
}

/**
 * Proposed, accepted, rejected — and where each figure comes from, because they
 * do not come from the same place.
 *
 * Acceptances are read from `tier_counts.LLM` on the wire. Rejections and
 * proposals are counted by reading exception rows. That asymmetry is not an
 * inconsistency to hide: an accepted hypothesis becomes a match, and a match is
 * not an exception, so an acceptance is structurally invisible to any scan of
 * the exception list. Naming which side each number came from is the difference
 * between an accounting and a total.
 */
export function HypothesisAccounting({
  metrics,
  census,
}: {
  metrics: Metrics;
  census: HypothesisCensus;
}) {
  const matched = metrics.tier_counts.LLM;
  // Three DISJOINT populations, and every proposal is in exactly one of them.
  // The third is the awkward one and is why the total is not simply accepted
  // plus rejected: a row can carry an accepted verdict and still be an
  // exception. Dropping it would make this table quietly undercount what the
  // analyst proposed, which is the number the whole view is about.
  const rows = [
    {
      id: "matched",
      label: "Accepted, and matched",
      value: matched,
      source: "tier_counts.LLM",
      note: "Survived existence, exclusivity, causality, arithmetic, coherence and uniqueness, and was tied to the bank line it named. These subjects are matches now and have left the exception list, which is why they cannot be counted from it.",
    },
    {
      id: "accepted-excepted",
      label: "Accepted, still an exception",
      value: census.accepted,
      source: "counted on the exception list",
      note: "Rows carrying an accepted verdict that are nevertheless still exceptions. The contract permits the shape — failed_check is documented as null when a hypothesis was accepted — but an acceptance normally produces a match, so a non-zero count here is worth opening rather than explaining away.",
    },
    {
      id: "rejected",
      label: "Rejected",
      value: census.rejected,
      source: "counted on the exception list",
      note: "The proposal, the reason and the machine-readable failed check all survive onto the exception, which is what makes a rejection inspectable rather than merely reported.",
    },
  ];
  const proposed = rows.reduce((sum, r) => sum + r.value, 0);

  return (
    <div className="space-y-4">
      <div className="overflow-x-auto rounded-xl border border-border">
        <table className="w-full min-w-[42rem] border-collapse text-left">
          <caption className="sr-only">
            Hypotheses proposed, accepted and rejected, with the source of each
            figure
          </caption>
          <thead>
            <tr className="border-b border-border bg-surface">
              <Th className="w-52 pl-4">Disposition</Th>
              <Th className="w-24 text-right">Count</Th>
              <Th className="w-52">Read from</Th>
              <Th>What it is</Th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.id} className="border-b border-border last:border-b-0">
                <td className="py-2.5 pr-4 pl-4 align-top text-xs font-medium">
                  {row.label}
                </td>
                <td className="tnum py-2.5 pr-4 text-right align-top text-sm font-medium">
                  {int(row.value)}
                </td>
                <td className="py-2.5 pr-4 align-top font-mono text-2xs text-muted-foreground">
                  {row.source}
                </td>
                <td className="py-2.5 pr-4 align-top text-2xs leading-relaxed text-muted-foreground">
                  {row.note}
                </td>
              </tr>
            ))}
          </tbody>
          <tfoot>
            <tr className="bg-surface">
              <td className="py-2.5 pr-4 pl-4 text-xs font-medium">Proposed</td>
              <td className="tnum py-2.5 pr-4 text-right text-sm font-medium">
                {int(proposed)}
              </td>
              <td className="py-2.5 pr-4 font-mono text-2xs text-muted-foreground">
                the three rows above
              </td>
              <td className="py-2.5 pr-4 text-2xs leading-relaxed text-muted-foreground">
                Added from counts of rows, never recovered by dividing the
                rejection rate into the accepted count. A derived integer that
                looks measured is worse than an absent one.
              </td>
            </tr>
          </tfoot>
        </table>
      </div>

      <p className="max-w-[72ch] text-xs leading-relaxed text-muted-foreground">
        A further <span className="tnum">{int(census.notAttempted)}</span>{" "}
        {census.notAttempted === 1 ? "subject" : "subjects"} on the exception
        list {census.notAttempted === 1 ? "carries" : "carry"} no hypothesis at
        all — the deterministic tiers declined them and the analyst never spoke
        about them. Those are not refusals and are deliberately outside the
        accounting above: nothing reached the verifier for them.
      </p>
    </div>
  );
}

/**
 * Which check refused each rejected hypothesis.
 *
 * All five wire spellings always render, zeros included — the same argument as
 * the tier counts. And the count under `existence` is explicitly a floor rather
 * than a decomposition: three separate rules report under that one label, and
 * separating them would mean parsing the free-text reason, which is prose
 * written for a human.
 */
export function RejectionBreakdown({ census }: { census: HypothesisCensus }) {
  const counted = VERIFIER_CHECKS.reduce(
    (sum, check) => sum + census.byCheck[check],
    0,
  );

  return (
    <div className="space-y-4">
      <div className="overflow-x-auto rounded-xl border border-border">
        <table className="w-full min-w-[46rem] border-collapse text-left">
          <caption className="sr-only">
            Rejections per verifier check, in the order the checks run
          </caption>
          <thead>
            <tr className="border-b border-border bg-surface">
              <Th className="w-12 pl-4">
                <span className="sr-only">Order</span>
              </Th>
              <Th className="w-32">Check</Th>
              <Th>What it asserts</Th>
              <Th className="w-28 pr-4 text-right">Rejections</Th>
            </tr>
          </thead>
          <tbody>
            {VERIFIER_CHECKS.map((check, i) => {
              const value = census.byCheck[check];
              const conflation = VERIFIER_CHECK_CONFLATION[check];
              return (
                <tr key={check} className="border-b border-border">
                  <td className="tnum py-3 pr-4 pl-4 align-top text-2xs text-muted-foreground">
                    {i + 1}
                  </td>
                  <td className="py-3 pr-4 align-top">
                    <span className="text-xs font-medium">
                      {VERIFIER_CHECK_LABEL[check]}
                    </span>
                    <code className="mt-0.5 block font-mono text-2xs text-muted-foreground">
                      {check}
                    </code>
                  </td>
                  <td className="py-3 pr-4 align-top text-xs leading-relaxed">
                    {VERIFIER_CHECK_DESCRIPTION[check]}
                    {conflation ? (
                      <span className="mt-1.5 block max-w-[62ch] text-2xs leading-relaxed text-muted-foreground">
                        {conflation}
                      </span>
                    ) : null}
                  </td>
                  <td
                    className={cn(
                      "tnum py-3 pr-4 text-right align-top text-sm font-medium",
                      value > 0 && "text-rejected-fg",
                    )}
                  >
                    {int(value)}
                  </td>
                </tr>
              );
            })}
            {census.rejectedWithoutCheck > 0 ? (
              <tr className="border-b border-border">
                <td className="py-3 pr-4 pl-4 align-top text-2xs text-muted-foreground">
                  —
                </td>
                <td className="py-3 pr-4 align-top text-xs font-medium">
                  No check named
                </td>
                <td className="py-3 pr-4 align-top text-xs leading-relaxed">
                  Rejected with{" "}
                  <code className="font-mono">failed_check</code> null. The
                  contract documents that field as null only when a hypothesis
                  was accepted or none was attempted, so a rejection without one
                  is a shape worth opening rather than counting.
                </td>
                <td className="tnum py-3 pr-4 text-right align-top text-sm font-medium text-error-fg">
                  {int(census.rejectedWithoutCheck)}
                </td>
              </tr>
            ) : null}
          </tbody>
          <tfoot>
            <tr className="bg-surface">
              <td />
              <td className="py-2.5 pr-4 text-xs font-medium">Rejections</td>
              <td className="py-2.5 pr-4 text-2xs text-muted-foreground">
                Every rejection carries exactly one check, so these sum to the
                rejected count
              </td>
              <td className="tnum py-2.5 pr-4 text-right text-xs font-medium">
                {int(counted + census.rejectedWithoutCheck)}
              </td>
            </tr>
          </tfoot>
        </table>
      </div>

      <p className="max-w-[72ch] text-xs leading-relaxed text-muted-foreground">
        The verifier returns on the first failure, so a rejection names the
        earliest rule a hypothesis broke rather than every rule it broke. These
        are counts of first failures, and a hypothesis that would also have
        failed a later check never reached it.
      </p>
    </div>
  );
}

function Th({
  children,
  className,
}: {
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <th
      scope="col"
      className={cn(
        "py-2.5 pr-4 text-2xs font-medium text-muted-foreground",
        className,
      )}
    >
      {children}
    </th>
  );
}

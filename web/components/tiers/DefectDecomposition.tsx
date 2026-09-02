import { CheckIcon, TriangleAlertIcon } from "lucide-react";
import {
  DEFECT_CLASSES,
  REFERENCE_BANK_LINES,
  REFERENCE_EXCEPTIONS,
  REFERENCE_MATCHES,
  REFERENCE_RECORDS,
  REFERENCE_SEED,
  REFERENCE_TIER_COUNTS,
  type TierKey,
} from "@/lib/tiers";
import { cn } from "@/lib/utils";
import type { Metrics } from "@/lib/types";

const int = (n: number) => n.toLocaleString("en-IN");

/**
 * What the tier counts decompose onto.
 *
 * The interesting fact about this ladder is not that T0 is large. It is that
 * the three small rungs land on exactly one defect class each, with no
 * remainder: T1 and T2 together are precisely the garbled narrations — garbling
 * is the only defect that removes a settlement reference from a line that is
 * still resolvable, so it is the only thing that ever reaches a reconstruction
 * rung — and T3 is precisely the rounding breaks.
 *
 * Both of those are ARITHMETIC, so this component performs it rather than
 * asserting it in prose. If a figure in `lib/tiers.ts` is ever edited out of
 * agreement with the reference tier counts beside it, the check below goes red
 * and says which side moved. A hand-written "17 + 83 = 100" is a sentence that
 * cannot fail.
 *
 * And this is a measurement of ONE dataset — seed 42, deterministic-only, at
 * 5,000 records — not of the run being viewed. `tier_counts` is the only tier
 * field on the wire and it carries no attribution to a defect class, so a live
 * decomposition is not something this console could compute. The heading says
 * whose numbers these are, and the last paragraph puts this run's three small
 * rungs beside them without claiming they decompose the same way.
 */
export function DefectDecomposition({
  tierCounts,
}: {
  tierCounts: Metrics["tier_counts"];
}) {
  const reconstruction =
    REFERENCE_TIER_COUNTS.T1 + REFERENCE_TIER_COUNTS.T2;
  const garbled = instancesReaching(["T1", "T2"]);
  const rounding = instancesReaching(["T3"]);

  const checks = [
    {
      id: "reconstruction",
      holds: reconstruction === garbled,
      statement: (
        <>
          T1 <span className="tnum">{int(REFERENCE_TIER_COUNTS.T1)}</span> + T2{" "}
          <span className="tnum">{int(REFERENCE_TIER_COUNTS.T2)}</span> ={" "}
          <span className="tnum">{int(reconstruction)}</span>, which is every{" "}
          <code className="font-mono">garbled_narration</code> line and nothing
          else.
        </>
      ),
      broken: (
        <>
          T1 + T2 sums to{" "}
          <span className="tnum">{int(reconstruction)}</span> but{" "}
          <code className="font-mono">garbled_narration</code> is recorded at{" "}
          <span className="tnum">{int(garbled)}</span>. The two sides of this
          decomposition disagree, so one of the figures in this table is stale.
        </>
      ),
    },
    {
      id: "tolerance",
      holds: REFERENCE_TIER_COUNTS.T3 === rounding,
      statement: (
        <>
          T3 <span className="tnum">{int(REFERENCE_TIER_COUNTS.T3)}</span> is
          every <code className="font-mono">rounding_break</code> line and
          nothing else — T0&apos;s reference lookup hits, T0 declines on the
          residual, and the tolerance rung takes it.
        </>
      ),
      broken: (
        <>
          T3 reports <span className="tnum">{int(REFERENCE_TIER_COUNTS.T3)}</span>{" "}
          against <span className="tnum">{int(rounding)}</span>{" "}
          <code className="font-mono">rounding_break</code> instances. One of the
          two figures is stale.
        </>
      ),
    },
  ];

  return (
    <section aria-labelledby="decomposition-heading" className="space-y-6">
      <div className="space-y-2">
        <h2
          id="decomposition-heading"
          className="text-base font-medium tracking-tight"
        >
          What the rungs decompose onto
        </h2>
        <p className="max-w-[72ch] text-xs leading-relaxed text-muted-foreground">
          The generator injects ten labelled defect classes, and the tier
          distribution lands on them with nothing left over. Everything in this
          table is a measurement of seed{" "}
          <span className="tnum">{REFERENCE_SEED}</span> at{" "}
          <span className="tnum">{int(REFERENCE_RECORDS)}</span> records, run
          deterministic-only — <span className="text-foreground">not</span> of
          the run you are looking at. Nothing on the wire attributes a match to a
          defect class, so no console can compute this live, and inventing it
          would be worse than naming its source.
        </p>
      </div>

      <div className="overflow-x-auto rounded-xl border border-border">
        <table className="w-full min-w-[46rem] border-collapse text-left">
          <caption className="sr-only">
            The ten injected defect classes and the tier that resolves each, at
            seed {REFERENCE_SEED}, {REFERENCE_RECORDS} records
          </caption>
          <thead>
            <tr className="border-b border-border bg-surface">
              <Th className="w-56 pl-4">Defect class</Th>
              <Th>What it tests</Th>
              <Th className="w-32">Resolved by</Th>
              <Th className="w-28 pr-4 text-right">Instances</Th>
            </tr>
          </thead>
          <tbody>
            {DEFECT_CLASSES.map((defect) => (
              <tr
                key={defect.name}
                className="border-b border-border last:border-b-0"
              >
                <td className="py-3 pr-4 pl-4 align-top">
                  <code className="font-mono text-xs break-all">
                    {defect.name}
                  </code>
                </td>
                <td className="py-3 pr-4 align-top text-xs leading-relaxed">
                  {defect.tests}
                  {defect.split ? (
                    <span className="mt-1 block text-2xs leading-relaxed text-muted-foreground">
                      {defect.split}
                    </span>
                  ) : null}
                  {defect.outcome ? (
                    <span className="mt-1 block max-w-[62ch] text-2xs leading-relaxed text-muted-foreground">
                      {defect.outcome}
                    </span>
                  ) : null}
                </td>
                <td className="py-3 pr-4 align-top">
                  {defect.resolvedBy.length === 0 ? (
                    <span className="text-2xs text-excepted-fg">Nothing</span>
                  ) : (
                    <span className="flex flex-wrap gap-1">
                      {defect.resolvedBy.map((tier) => (
                        <code
                          key={tier}
                          className="rounded-sm border border-border px-1.5 py-px font-mono text-2xs"
                        >
                          {tier}
                        </code>
                      ))}
                    </span>
                  )}
                </td>
                <td className="tnum py-3 pr-4 text-right align-top text-xs font-medium">
                  {int(defect.instances)}
                  {defect.subjects ? (
                    <span className="mt-0.5 block text-2xs font-normal text-muted-foreground">
                      {int(defect.subjects)} bank lines
                    </span>
                  ) : null}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <ul className="max-w-[72ch] space-y-3">
        {checks.map((check) => (
          <Check key={check.id} holds={check.holds}>
            {check.holds ? check.statement : check.broken}
          </Check>
        ))}
      </ul>

      <p className="max-w-[72ch] text-xs leading-relaxed text-muted-foreground">
        T0 takes the remainder — every clean settlement plus the six classes that
        damage a settlement&apos;s content while leaving its reference intact and
        its sum exact — and on that reference run it carries{" "}
        <span className="tnum">{int(REFERENCE_TIER_COUNTS.T0)}</span> of{" "}
        <span className="tnum">{int(REFERENCE_MATCHES)}</span> matches over{" "}
        <span className="tnum">{int(REFERENCE_BANK_LINES)}</span> bank lines,
        against <span className="tnum">{int(REFERENCE_EXCEPTIONS)}</span>{" "}
        exceptions. The two classes nothing resolves are the whole bank-line
        exception list — 100 subjects each — and the remaining{" "}
        <span className="tnum">100</span> exceptions are the suppressed copies of
        duplicated legs, which are diagnostics and sit outside every rate&apos;s
        denominator. One of the two is a trap the system is supposed to decline;
        the other is the entire distance between the match rate and 1.0.
      </p>

      <p className="max-w-[72ch] text-xs leading-relaxed text-muted-foreground">
        <span className="font-medium text-foreground">
          This run reports T1 <span className="tnum">{int(tierCounts.T1)}</span>,
          T2 <span className="tnum">{int(tierCounts.T2)}</span>, T3{" "}
          <span className="tnum">{int(tierCounts.T3)}</span>.
        </span>{" "}
        Whether they decompose the same way is not something this page can check:
        it would need a per-match defect label, and the contract has no field
        that carries one. The rules above hold for any run; the counts beside
        them are one dataset&apos;s.
      </p>
    </section>
  );
}

/**
 * Instances of every defect class that is resolved by these rungs and by no
 * other. Counted once per class, so a class that two rungs share — which
 * `garbled_narration` is, split by payment-leg cardinality — is not counted
 * twice into its own total.
 */
function instancesReaching(tiers: readonly TierKey[]): number {
  return DEFECT_CLASSES.filter(
    (d) => d.resolvedBy.length > 0 && d.resolvedBy.every((t) => tiers.includes(t)),
  ).reduce((sum, d) => sum + d.instances, 0);
}

function Check({
  holds,
  children,
}: {
  holds: boolean;
  children: React.ReactNode;
}) {
  const Icon = holds ? CheckIcon : TriangleAlertIcon;
  return (
    <li
      className={cn(
        "flex items-start gap-2 text-xs leading-relaxed",
        holds ? "text-muted-foreground" : "text-error-fg",
      )}
    >
      <Icon
        aria-hidden
        className={cn("mt-0.5 size-3.5 shrink-0", holds && "text-matched")}
        strokeWidth={2}
      />
      <span>{children}</span>
    </li>
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

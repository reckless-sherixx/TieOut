"use client";

import * as React from "react";
import { ChevronRightIcon } from "lucide-react";
import { Caveat } from "@/components/ui/Caveat";
import { confidenceLabel, type TierConfidence } from "@/lib/confidence";
import { formatRate } from "@/lib/money";
import {
  REFERENCE_RECORDS,
  REFERENCE_SEED,
  TIER_KEYS,
  TIERS,
  type TierKey,
} from "@/lib/tiers";
import { cn } from "@/lib/utils";
import type { Metrics } from "@/lib/types";

const int = (n: number) => n.toLocaleString("en-IN");

/**
 * The tier walk: five rungs, what each one requires, and what it produced here.
 *
 * ALL FIVE ROWS ALWAYS RENDER, INCLUDING THE ZEROS, and a zero row says in the
 * row itself that nothing reached that rung rather than leaving the reader to
 * infer it from an empty bar. `T2: 0` on a small dataset and `LLM: 0` on a
 * deterministic run are correct results; the contract guarantees all five keys
 * are present precisely so that "this rung matched nothing" and "we do not know
 * what this rung did" stay different claims.
 *
 * Each row opens the rung's own requirement — the same rule as every number on
 * the summary opening its own arithmetic. What a count means is the rule that
 * produced it, so the rule lives where the count is.
 */
export function TierLadder({
  tierCounts,
  tierConfidence,
  matchCount,
}: {
  tierCounts: Metrics["tier_counts"];
  /**
   * The confidence the ENGINE stamped, per tier, on this run. Undefined on a
   * run stored before the field existed — which renders as "not reported",
   * never as a plausible-looking constant. A hardcoded table here is what
   * rendered "verified" on the rung the engine stamps 0.70.
   */
  tierConfidence?: Partial<Record<TierKey, TierConfidence>>;
  /** `match_count` from the wire — a second, independent field to check against. */
  matchCount: number;
}) {
  const [open, setOpen] = React.useState<TierKey | null>(null);

  const values = TIER_KEYS.map((key) => tierCounts[key]);
  const total = values.reduce((sum, n) => sum + n, 0);
  // Scaled to the largest rung, never to the total: the distribution is
  // lopsided by construction, and scaling to the total rounds the small rungs
  // to nothing. The guard keeps a run that matched nothing from dividing by 0.
  const max = Math.max(1, ...values);
  const zeroTiers = TIER_KEYS.filter((key) => tierCounts[key] === 0);
  // "T2, LLM produced nothing" reads as a fragment; "T2 and LLM" reads as a
  // sentence. Written out because a list of two is the common case here.
  const zeroList =
    zeroTiers.length <= 1
      ? zeroTiers.join("")
      : `${zeroTiers.slice(0, -1).join(", ")} and ${zeroTiers[zeroTiers.length - 1]}`;

  return (
    <div className="space-y-5">
      {/* `relative` is load-bearing. The sr-only span in the fourth header
          cell is absolutely positioned; with no positioned ancestor it
          resolves against the document and drags it to the table's full
          width -- measured 425px inside a 375px viewport. */}
      <div className="relative overflow-x-auto rounded-xl border border-border">
        <table className="w-full min-w-[46rem] border-collapse text-left">
          <caption className="sr-only">
            Matches produced per tier, each row expandable to the rule that tier
            applies
          </caption>
          <thead>
            <tr className="border-b border-border bg-surface">
              <Th className="w-9 pl-4">
                <span className="sr-only">Expand</span>
              </Th>
              <Th className="w-16">Tier</Th>
              <Th>Requires</Th>
              <Th className="w-40">
                <span className="sr-only">Relative size</span>
              </Th>
              <Th className="w-24 text-right">Matches</Th>
              {/* Named for its denominator, and it is a DIFFERENT denominator
                  from the identically-shaped table on the summary: T0 is 89.4%
                  of matches here and 69.6% of subjects there. Both tables name
                  their denominator in the Total row, so neither was wrong --
                  but one column header on two tabs a click apart invited the
                  comparison. */}
              <Th className="w-28 pr-4 text-right">Share of matches</Th>
            </tr>
          </thead>

          <TierGroup
            label="Deterministic — no model involved"
            keys={["T0", "T1", "T2", "T3"]}
            tierCounts={tierCounts}
            tierConfidence={tierConfidence}
            total={total}
            max={max}
            open={open}
            onToggle={(key) => setOpen((cur) => (cur === key ? null : key))}
          />
          <TierGroup
            label="LLM-assisted — proposed by the analyst, accepted by the verifier"
            keys={["LLM"]}
            tierCounts={tierCounts}
            tierConfidence={tierConfidence}
            total={total}
            max={max}
            open={open}
            onToggle={(key) => setOpen((cur) => (cur === key ? null : key))}
          />

          <tfoot>
            <tr className="bg-surface">
              <td />
              <td className="py-2.5 text-xs font-medium">All</td>
              <td className="py-2.5 pr-4 text-xs text-muted-foreground">
                Match groups produced, from the engine&apos;s own tier assignment
              </td>
              <td />
              <td className="tnum py-2.5 text-right text-xs font-medium">
                {int(total)}
              </td>
              <td className="tnum py-2.5 pr-4 text-right text-xs text-muted-foreground">
                {total === 0 ? "—" : "100.0%"}
              </td>
            </tr>
          </tfoot>
        </table>
      </div>

      <SumCheck total={total} matchCount={matchCount} />

      <p className="max-w-[72ch] text-xs leading-relaxed text-muted-foreground">
        {zeroTiers.length === 0 ? (
          <>
            Every rung produced at least one match on this run.
          </>
        ) : (
          <>
            <span className="font-medium text-foreground">
              {zeroList} produced nothing on this run, and{" "}
              {zeroTiers.length === 1 ? "that row is" : "those rows are"} a
              result rather than a gap.
            </span>{" "}
            All five keys are always present on the wire, so a rung that matched
            nothing is reported as an empty track and a zero and is never
            omitted. Open {zeroTiers.length === 1 ? "it" : "them"} for what the
            data would have had to contain for{" "}
            {zeroTiers.length === 1 ? "it" : "them"} to fire.
          </>
        )}
      </p>

      <Caveat summary="What the bars are scaled to, and what a row's defect classes are not">
        <p>
          Bars are scaled to the largest rung rather than to the total. The
          distribution is lopsided by construction, and scaling to the total
          would round the small rungs away — a rung carrying two matches against
          a thousand has to stay visible.
        </p>
        <p>
          Defect classes named inside a row are a reference measurement of seed{" "}
          <span className="tnum">{REFERENCE_SEED}</span> at{" "}
          <span className="tnum">{int(REFERENCE_RECORDS)}</span> records. The
          wire carries no attribution of a match to a defect class, so they are
          never a claim about this run.
        </p>
      </Caveat>
    </div>
  );
}

function TierGroup({
  label,
  keys,
  tierCounts,
  tierConfidence,
  total,
  max,
  open,
  onToggle,
}: {
  label: string;
  keys: readonly TierKey[];
  tierCounts: Metrics["tier_counts"];
  tierConfidence?: Partial<Record<TierKey, TierConfidence>>;
  total: number;
  max: number;
  open: TierKey | null;
  onToggle: (key: TierKey) => void;
}) {
  return (
    <tbody>
      <tr>
        <th
          scope="colgroup"
          colSpan={6}
          className="border-b border-border bg-surface/60 px-4 py-1.5 text-left text-2xs font-medium text-muted-foreground"
        >
          {label}
        </th>
      </tr>
      {keys.map((key) => (
        <TierRow
          key={key}
          tier={TIERS[key]}
          value={tierCounts[key]}
          confidence={tierConfidence?.[key]}
          total={total}
          max={max}
          open={open === key}
          onToggle={() => onToggle(key)}
        />
      ))}
    </tbody>
  );
}

function TierRow({
  tier,
  value,
  confidence,
  total,
  max,
  open,
  onToggle,
}: {
  tier: (typeof TIERS)[TierKey];
  value: number;
  confidence?: TierConfidence;
  total: number;
  max: number;
  open: boolean;
  onToggle: () => void;
}) {
  const panelId = `tier-${tier.key}`;
  // A zero is genuinely zero-width. A non-zero rung is floored at a visible
  // sliver so that two matches against a thousand cannot read as nothing at
  // all — the one thing this bar must never do.
  const width =
    value === 0 ? "0%" : `max(0.375rem, ${((value / max) * 100).toFixed(3)}%)`;

  return (
    <>
      <tr
        className={cn(
          "border-b border-border transition-colors duration-150",
          open ? "bg-surface-selected" : "hover:bg-surface-hover",
        )}
      >
        <td className="py-0 pl-4">
          <button
            type="button"
            aria-expanded={open}
            aria-controls={panelId}
            onClick={onToggle}
            className="flex size-6 items-center justify-center rounded-md text-muted-foreground transition-colors duration-150 hover:bg-surface-active hover:text-foreground focus-visible:focus-ring"
          >
            <ChevronRightIcon
              aria-hidden
              strokeWidth={2}
              className={cn(
                "size-3.5 transition-transform duration-150",
                open && "rotate-90",
              )}
            />
            <span className="sr-only">
              {open ? "Hide" : "Show"} what {tier.key} requires
            </span>
          </button>
        </td>

        <td className="py-2.5 align-top">
          <code className="font-mono text-xs font-medium tracking-tight">
            {tier.key}
          </code>
        </td>

        <td className="py-2.5 pr-4 align-top text-xs">
          <span className="text-foreground">{tier.rule}</span>
          {value === 0 ? (
            <span className="mt-1 block text-2xs leading-relaxed text-muted-foreground">
              Nothing reached this rung on this run. That is a measurement, not
              a missing row — open it for what the data would have had to
              contain.
            </span>
          ) : null}
        </td>

        <td className="py-2.5 pr-4 align-middle">
          <div
            aria-hidden
            className="h-2 w-full overflow-hidden rounded-full bg-muted"
          >
            {/* The "matched" outcome colour, not the accent: the accent is
                reserved for emphasis and this is a measurement of an outcome. */}
            <div className="h-full rounded-full bg-matched" style={{ width }} />
          </div>
        </td>

        <td className="tnum py-2.5 text-right align-top text-sm font-medium">
          {int(value)}
        </td>

        <td className="tnum py-2.5 pr-4 text-right align-top text-2xs text-muted-foreground">
          {total === 0 ? "—" : formatRate(value / total)}
        </td>
      </tr>

      <tr id={panelId} className={cn("border-b border-border", !open && "hidden")}>
        <td colSpan={6} className="px-4 py-6 sm:px-6">
          {open ? (
            <TierDetail tier={tier} value={value} confidence={confidence} />
          ) : null}
        </td>
      </tr>
    </>
  );
}

function TierDetail({
  tier,
  value,
  confidence,
}: {
  tier: (typeof TIERS)[TierKey];
  value: number;
  confidence?: TierConfidence;
}) {
  const label = confidenceLabel(confidence);
  return (
    <div className="grid gap-8 lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
      <div>
        <Caption>What this rung requires</Caption>
        <dl className="mt-2.5 space-y-2.5">
          {tier.requires.map((req) => (
            <div key={req.label}>
              <div className="flex flex-wrap items-baseline gap-x-3 gap-y-0.5">
                <dt className="text-2xs text-muted-foreground">{req.label}</dt>
                <dd className="text-xs font-medium">{req.value}</dd>
              </div>
              {req.note ? (
                <p className="mt-0.5 max-w-[62ch] text-2xs leading-relaxed text-muted-foreground">
                  {req.note}
                </p>
              ) : null}
            </div>
          ))}
          <div>
            <div className="flex flex-wrap items-baseline gap-x-3 gap-y-0.5">
              <dt className="text-2xs text-muted-foreground">
                Confidence the engine stamped
              </dt>
              <dd className="tnum text-xs font-medium">{label.value}</dd>
            </div>
            <p className="mt-0.5 max-w-[62ch] text-2xs leading-relaxed text-muted-foreground">
              {label.meaning}
            </p>
          </div>
        </dl>
      </div>

      <div className="space-y-6">
        <div>
          <Caption>Falls through when</Caption>
          <p className="mt-2 max-w-[62ch] text-xs leading-relaxed">
            {tier.declines}
          </p>
        </div>

        <div>
          <Caption>Defect classes it resolves</Caption>
          <p className="mt-2 max-w-[62ch] text-xs leading-relaxed">
            {tier.defects}
          </p>
          <p className="mt-1.5 max-w-[62ch] text-2xs leading-relaxed text-muted-foreground">
            Measured on seed <span className="tnum">{REFERENCE_SEED}</span> at{" "}
            <span className="tnum">{int(REFERENCE_RECORDS)}</span> records. Not a
            claim about this run: nothing on the wire attributes a match to a
            defect class.
          </p>
        </div>

        {value === 0 ? (
          <div className="border-l border-border pl-4">
            <Caption>Why this row reads zero</Caption>
            <p className="mt-2 max-w-[62ch] text-xs leading-relaxed">
              {tier.zeroMeans}
            </p>
          </div>
        ) : null}
      </div>
    </div>
  );
}

/**
 * The five tier counts and `match_count` are two independent fields on the
 * wire. Their agreement is checkable, so it is checked here rather than
 * assumed — and a disagreement means one of the two is wrong.
 */
function SumCheck({ total, matchCount }: { total: number; matchCount: number }) {
  const agrees = total === matchCount;
  return (
    <p
      className={cn(
        "max-w-[72ch] text-xs leading-relaxed",
        agrees ? "text-muted-foreground" : "text-error-fg",
      )}
    >
      {agrees ? (
        <>
          The five counts sum to{" "}
          <span className="tnum font-mono">{int(total)}</span>, which is exactly{" "}
          <code className="font-mono">match_count</code>. Two independent fields
          on the wire agree, so the breakdown accounts for every match this run
          produced and there is no residual rung.
        </>
      ) : (
        <>
          <span className="font-medium">These counts do not add up.</span> The
          five tiers sum to <span className="tnum font-mono">{int(total)}</span>{" "}
          but <code className="font-mono">match_count</code> reports{" "}
          <span className="tnum font-mono">{int(matchCount)}</span>. Two fields
          that must agree do not, so one of them is wrong — treat every share on
          this page as unreliable, and check the run&apos;s scorecard before
          quoting anything from it.
        </>
      )}
    </p>
  );
}

function Caption({ children }: { children: React.ReactNode }) {
  return (
    <p className="text-2xs font-medium text-muted-foreground">{children}</p>
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

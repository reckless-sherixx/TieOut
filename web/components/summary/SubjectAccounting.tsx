"use client";

import { CheckIcon, TriangleAlertIcon } from "lucide-react";
import { formatRate } from "@/lib/money";
import { isTerminal } from "@/lib/labels";
import {
  useSubjectAccounting,
  type SubjectAccountingData,
} from "@/lib/subjects";
import { cn } from "@/lib/utils";
import { Caveat } from "@/components/ui/Caveat";
import type { Metrics, RunSummary } from "@/lib/types";

const int = (n: number) => n.toLocaleString("en-IN");

type TierKey = keyof Metrics["tier_counts"];

/**
 * The rule each tier applies, from the tier ladder. One line, because this
 * panel is the accounting and /runs/[id]/tiers is the tier ladder.
 */
const TIER_RULE: Record<TierKey, string> = {
  T0: "Reference hit and exact arithmetic",
  T1: "One payment leg, exact, in window",
  T2: "Two or more payment legs, exact, in window",
  T3: "Within ±₹1 tolerance, any cardinality",
  LLM: "Proposed by the analyst, accepted by the verifier",
};

const TIER_KEYS: readonly TierKey[] = ["T0", "T1", "T2", "T3", "LLM"];

/**
 * Where every subject went.
 *
 * The engine's central invariant is a partition: every subject is matched or
 * excepted, exactly once, never both and never neither. That is what makes
 * the rates on this page measurements rather than claims, so the console
 * renders it as an accounting that visibly sums — with no residual bucket and
 * no "other" — and checks the sum live rather than asserting it.
 *
 * ALL FIVE TIER KEYS ALWAYS RENDER, ZEROS INCLUDED. "T2 matched nothing" and
 * "we do not know what T2 did" are different claims and only the first is
 * renderable; an omitted row turns a result into a silence.
 *
 * THE IDENTITY THIS TABLE RENDERS, WRITTEN OUT BECAUSE IT USED TO BE WRONG:
 *
 *     T0 + T1 + T2 + T3 + LLM + exception_count
 *       ==  match_count + exception_count
 *       ==  SUBJECTS accounted for                      (181 on seed 42 / 500)
 *
 * That total is the SUBJECT count. It is not the bank-line count, and this
 * table's Total row said "Bank lines this run accounted for" until it was
 * caught reading 181 against the Records tab's 171. Matches are all bank-line
 * subjects; exceptions are raised against bank lines AND against PSP
 * transactions, so the two quantities differ by exactly the PSP-side
 * exceptions. `BankLineReconciliation` below renders the other identity —
 *
 *     match_count + bank-line exceptions  ==  bank lines in the run  (171)
 *
 * — and checks it live against the records listing rather than asserting it.
 */
export function SubjectAccounting({
  run,
  metrics,
}: {
  run: RunSummary;
  metrics: Metrics;
}) {
  const tiers = TIER_KEYS.map((key) => ({ key, value: metrics.tier_counts[key] }));
  const tierTotal = tiers.reduce((sum, t) => sum + t.value, 0);
  const accounted = run.match_count + run.exception_count;
  const sumsExactly = tierTotal === run.match_count;

  const rows = [
    ...tiers.map((t) => ({
      id: t.key,
      label: t.key,
      note: TIER_RULE[t.key],
      value: t.value,
      tone: "matched" as const,
    })),
    {
      id: "exceptions",
      label: "Exceptions",
      note: "Itemised, each with a machine-readable reason code",
      value: run.exception_count,
      tone: "excepted" as const,
    },
  ];

  return (
    <section aria-labelledby="accounting-heading" className="space-y-6">
      <div className="space-y-2">
        <h2
          id="accounting-heading"
          className="text-base font-medium tracking-tight"
        >
          Where every subject went
        </h2>
        <p className="max-w-[72ch] text-xs leading-relaxed text-muted-foreground">
          Every subject is matched or excepted, exactly once — never both, never
          neither.
        </p>
        <Caveat summary="How to read this partition">
          <p>
            The rows below are that partition, and there is no residual bucket.
            All five tiers appear including the zeros: a tier that matched
            nothing is a result, and an absent row would be a silence.
          </p>
          <p>
            A subject here is a bank line <em>or</em> a PSP transaction. The
            count of bank lines alone is the smaller number, and it is
            reconciled beneath the table — which is the whole of the difference
            between the two totals on this page.
          </p>
        </Caveat>
      </div>

      <Rail rows={rows} total={accounted} />

      <div className="overflow-x-auto">
        <table className="w-full min-w-[34rem] border-collapse text-left">
          <caption className="sr-only">
            Subject disposition by tier and by exception
          </caption>
          <thead>
            <tr className="border-b border-border">
              <Th className="w-24">Disposition</Th>
              <Th>Rule</Th>
              <Th className="w-24 text-right">Subjects</Th>
              {/* Named for its denominator. The tier ladder one tab away shows
                  a share of MATCHES for the same five rows — T0 is 69.6% here
                  and 89.4% there — and two columns headed "Share" on adjacent
                  tabs invite a comparison that means nothing. */}
              <Th className="w-32 text-right">Share of subjects</Th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.id} className="border-b border-border/60">
                <td className="py-2.5 pr-4">
                  <span className="inline-flex items-center gap-2">
                    <span
                      aria-hidden
                      className={cn(
                        "size-1.5 shrink-0 rounded-[2px]",
                        row.tone === "matched" ? "bg-matched" : "bg-excepted",
                      )}
                    />
                    <span
                      className={cn(
                        "text-xs font-medium",
                        row.id === "exceptions" ? "" : "font-mono",
                      )}
                    >
                      {row.label}
                    </span>
                  </span>
                </td>
                <td className="py-2.5 pr-4 text-xs text-muted-foreground">
                  {row.note}
                </td>
                <td className="tnum py-2.5 text-right text-xs font-medium">
                  {int(row.value)}
                </td>
                <td className="tnum py-2.5 text-right text-xs text-muted-foreground">
                  {accounted === 0 ? "—" : formatRate(row.value / accounted)}
                </td>
              </tr>
            ))}
          </tbody>
          <tfoot>
            <tr>
              <td className="py-2.5 pr-4 text-xs font-medium">Total</td>
              <td className="py-2.5 pr-4 text-xs text-muted-foreground">
                Subjects this run accounted for —{" "}
                <code className="font-mono">match_count</code> +{" "}
                <code className="font-mono">exception_count</code>
              </td>
              <td className="tnum py-2.5 text-right text-xs font-medium">
                {int(accounted)}
              </td>
              <td className="tnum py-2.5 text-right text-xs text-muted-foreground">
                {accounted === 0 ? "—" : "100.0%"}
              </td>
            </tr>
          </tfoot>
        </table>
      </div>

      <SumCheck
        sumsExactly={sumsExactly}
        tierTotal={tierTotal}
        matchCount={run.match_count}
      />

      <BankLineReconciliation run={run} />
    </section>
  );
}

/**
 * The same run, counted in bank lines instead of in subjects.
 *
 * Two independent measurements, set against each other rather than one
 * asserted:
 *
 *   - `match_count` plus the exceptions whose subject is a bank line, taken
 *     from the eight server-side reason-code totals the exception list already
 *     reads;
 *   - the run's bank lines, taken from `records?source=bank_line`, which is
 *     literally the number the Records tab prints.
 *
 * They must be equal, because a bank line is either matched or excepted and
 * every match is a bank-line subject. If they are not, that is a finding and
 * this panel says so instead of picking one.
 */
function BankLineReconciliation({ run }: { run: RunSummary }) {
  const { data, error, loading } = useSubjectAccounting(
    run.run_id,
    run.state,
    isTerminal(run.state),
  );

  return (
    <div className="max-w-[72ch] brut bg-surface px-5 py-4">
      <p className="text-xs font-medium">The same run, counted in bank lines</p>

      {error ? (
        <p className="mt-2 text-xs leading-relaxed text-muted-foreground">
          The split by subject type did not load, so it is absent rather than
          guessed. The subject total above is unaffected — it comes from{" "}
          <code className="font-mono">match_count</code> and{" "}
          <code className="font-mono">exception_count</code> on the run summary,
          which is already on screen. The Records tab reports the bank-line
          count directly.
          <br />
          <span className="font-mono text-2xs break-words">{error.message}</span>
        </p>
      ) : loading || data === null ? (
        <p className="mt-2 text-xs leading-relaxed text-muted-foreground" role="status">
          Reading the eight reason-code totals and the bank-line record count
          from the server. Until both land there is no split to show, and an
          assumed one would be worse than a wait.
        </p>
      ) : (
        <Reconciled run={run} data={data} />
      )}
    </div>
  );
}

function Reconciled({
  run,
  data,
}: {
  run: RunSummary;
  data: SubjectAccountingData;
}) {
  // THE IDENTITY. Every match is a bank-line subject and every bank line is
  // either matched or excepted, so these two must be the same integer.
  const derived = run.match_count + data.bankLineExceptions;
  const holds = derived === data.bankLineRecords;
  const censusAgrees = data.censusTotal === run.exception_count;

  return (
    <>
      <dl className="mt-3 space-y-1.5">
        <Term
          label="Matches, every one of them a bank line"
          value={run.match_count}
        />
        <Term
          label="Exceptions raised against a bank line"
          value={data.bankLineExceptions}
          sign="+"
        />
        <Term
          label="Bank lines in this run"
          value={derived}
          sign="="
          rule
          strong
        />
        <Term
          label="Exceptions raised against a PSP transaction"
          value={data.pspExceptions}
          note="Subjects, and not bank lines — which is the whole of the difference between the two totals on this page."
        />
      </dl>

      <p
        className={cn(
          "mt-3 flex items-start gap-2 text-xs leading-relaxed",
          holds ? "text-muted-foreground" : "text-error-fg",
        )}
      >
        {holds ? (
          <CheckIcon
            aria-hidden
            className="mt-0.5 size-3.5 shrink-0 text-matched"
            strokeWidth={2}
          />
        ) : (
          <TriangleAlertIcon aria-hidden className="mt-0.5 size-3.5 shrink-0" strokeWidth={2} />
        )}
        <span>
          {holds ? (
            <>
              <span className="tnum font-mono">{int(derived)}</span> is exactly
              what{" "}
              <code className="font-mono">records?source=bank_line</code>{" "}
              reports, which is the figure on the Records tab. The subject total
              above is{" "}
              <span className="tnum font-mono">
                {int(run.match_count + run.exception_count)}
              </span>{" "}
              because {int(data.pspExceptions)} of the exceptions are PSP
              transactions rather than bank lines.
            </>
          ) : (
            <>
              These two disagree:{" "}
              <span className="tnum font-mono">{int(derived)}</span> from the
              matches and the bank-line reason codes, against{" "}
              <span className="tnum font-mono">
                {int(data.bankLineRecords)}
              </span>{" "}
              from <code className="font-mono">records?source=bank_line</code>.
              A bank line is matched or excepted and never neither, so one of
              the two is wrong. Treat the bank-line figure on this page as
              unreliable until it is resolved.
            </>
          )}
        </span>
      </p>

      {censusAgrees ? null : (
        <p className="mt-2 flex items-start gap-2 text-xs leading-relaxed text-error-fg">
          <TriangleAlertIcon aria-hidden className="mt-0.5 size-3.5 shrink-0" strokeWidth={2} />
          <span>
            The eight reason-code totals sum to{" "}
            <span className="tnum font-mono">{int(data.censusTotal)}</span> but{" "}
            <code className="font-mono">exception_count</code> reports{" "}
            <span className="tnum font-mono">{int(run.exception_count)}</span>.
            The split above is drawn from those totals, so read it knowing they
            do not account for every exception.
          </span>
        </p>
      )}
    </>
  );
}

function Term({
  label,
  value,
  sign,
  rule,
  strong,
  note,
}: {
  label: string;
  value: number;
  sign?: string;
  rule?: boolean;
  strong?: boolean;
  note?: string;
}) {
  return (
    <div className={cn(rule && "border-t border-border pt-1.5")}>
      <div className="flex items-baseline justify-between gap-x-4">
        <dt
          className={cn(
            "text-xs",
            strong ? "font-medium" : "text-muted-foreground",
          )}
        >
          <span aria-hidden className="mr-1.5 inline-block w-2 text-muted-foreground">
            {sign ?? ""}
          </span>
          {label}
        </dt>
        <dd
          className={cn("tnum text-xs", strong ? "font-medium" : "")}
        >
          {int(value)}
        </dd>
      </div>
      {note ? (
        <p className="mt-0.5 pl-[0.875rem] text-2xs leading-relaxed text-muted-foreground">
          {note}
        </p>
      ) : null}
    </div>
  );
}

function Rail({
  rows,
  total,
}: {
  rows: { id: string; label: string; value: number; tone: "matched" | "excepted" }[];
  total: number;
}) {
  if (total === 0) {
    return (
      <div
        aria-hidden
        className="h-3 w-full rounded-none border border-dashed border-border"
      />
    );
  }
  // Descending emphasis inside the matched zone: the tiers are ordered by how
  // much the engine had to infer, and the rail shows that ordering. Written
  // out in full rather than composed, because a class name assembled at
  // runtime is a class name Tailwind never sees and never emits.
  const MATCHED_TINTS = [
    "bg-matched",
    "bg-matched/80",
    "bg-matched/60",
    "bg-matched/45",
    "bg-matched/30",
  ];

  // Resolved before the map rather than counted during it: a running index
  // mutated inside a render callback is exactly the pattern that produces a
  // different result on a re-render.
  let matchedSeen = 0;
  const segments = rows.map((row) => ({
    ...row,
    className:
      row.tone === "matched"
        ? (MATCHED_TINTS[matchedSeen++] ?? "bg-matched/30")
        : "bg-excepted",
  }));

  return (
    <div
      className="flex h-3 w-full gap-px overflow-hidden rounded-none bg-muted"
      role="img"
      aria-label={rows
        .map((r) => `${r.label} ${r.value}`)
        .join(", ")
        .concat(`, of ${total} subjects`)}
    >
      {segments.map((segment) => (
        <div
          key={segment.id}
          className={segment.className}
          style={{ width: `${(segment.value / total) * 100}%` }}
          title={`${segment.label} · ${int(segment.value)}`}
        />
      ))}
    </div>
  );
}

/**
 * The tier counts are the engine's own record of which rung produced each
 * match, and `match_count` is its record of how many it produced. They come
 * from different fields on the wire, so their agreement is checkable — and a
 * disagreement would mean one of the two is wrong.
 */
function SumCheck({
  sumsExactly,
  tierTotal,
  matchCount,
}: {
  sumsExactly: boolean;
  tierTotal: number;
  matchCount: number;
}) {
  const Icon = sumsExactly ? CheckIcon : TriangleAlertIcon;
  return (
    <p
      className={cn(
        "flex max-w-[72ch] items-start gap-2 text-xs leading-relaxed",
        sumsExactly ? "text-muted-foreground" : "text-error-fg",
      )}
    >
      <Icon
        aria-hidden
        className={cn("mt-0.5 size-3.5 shrink-0", sumsExactly && "text-matched")}
        strokeWidth={2}
      />
      <span>
        {sumsExactly ? (
          <>
            The five tier counts sum to{" "}
            <span className="tnum font-mono">{int(tierTotal)}</span>, which is
            exactly <code className="font-mono">match_count</code>. Two
            independent fields on the wire agree.
          </>
        ) : (
          <>
            The five tier counts sum to{" "}
            <span className="tnum font-mono">{int(tierTotal)}</span> but{" "}
            <code className="font-mono">match_count</code> reports{" "}
            <span className="tnum font-mono">{int(matchCount)}</span>. Two
            fields that must agree do not, so one of them is wrong. Treat the
            tier breakdown on this page as unreliable until it is resolved.
          </>
        )}
      </span>
    </p>
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
        "pb-2 text-2xs font-medium text-muted-foreground",
        className,
      )}
    >
      {children}
    </th>
  );
}

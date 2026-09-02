"use client";

import { CheckIcon, TriangleAlertIcon } from "lucide-react";
import { useReasonCensus } from "@/lib/census";
import {
  REASON_CODES,
  REASON_CODE_DESCRIPTION,
  REASON_CODE_LABEL,
  UNRESOLVABLE_BY_DESIGN,
} from "@/lib/labels";
import { cn } from "@/lib/utils";
import type { ReasonCode, RunState } from "@/lib/types";
import { ErrorState } from "@/components/States";

const int = (n: number) => n.toLocaleString("en-IN");

/**
 * The reason-code filter, which is also the census.
 *
 * Every one of the eight codes is listed with how many rows it has in this run,
 * INCLUDING the ones with none. Five of the eight score zero on this generator,
 * and a zero beside a code means "this data did not produce it" rather than
 * "the system handles it" — a filter that quietly omitted the empty codes would
 * be making the second claim on the first's evidence.
 *
 * The counts come from the server, not from the page in the browser: one
 * request per code asking for a single row and reading `total` off the
 * pagination envelope. Eight tiny requests, and the client never holds more
 * than fifty rows to produce them.
 *
 * Their sum and `RunSummary.exception_count` are two independent facts, so the
 * agreement is checked rather than assumed.
 */
export function ReasonFilter({
  runId,
  runState,
  exceptionCount,
  value,
  onChange,
}: {
  runId: string;
  runState: RunState;
  /** `RunSummary.exception_count`, to check the census against. */
  exceptionCount: number;
  value: ReasonCode | null;
  onChange: (next: ReasonCode | null) => void;
}) {
  const { data, error, loading } = useReasonCensus(runId, runState);

  if (error) {
    return (
      <ErrorState
        title="The reason-code counts did not load"
        error={error}
        recovery={
          <>
            Each count is a separate request to{" "}
            <code className="font-mono">
              GET /api/runs/{runId}/exceptions?reason_code=…
            </code>{" "}
            asking for one row, and at least one of them failed. The list below
            still works unfiltered; reload the view to try the counts again.
          </>
        }
      />
    );
  }

  const agrees = data !== null && data.total === exceptionCount;

  return (
    <div className="space-y-3">
      <div
        role="group"
        aria-label="Filter exceptions by reason code"
        className="flex flex-wrap gap-1.5"
      >
        <FilterChip
          label="All reason codes"
          count={data?.total ?? null}
          loading={loading}
          selected={value === null}
          onSelect={() => onChange(null)}
        />
        {REASON_CODES.map((code) => (
          <FilterChip
            key={code}
            label={REASON_CODE_LABEL[code]}
            title={REASON_CODE_DESCRIPTION[code]}
            count={data?.byReason[code] ?? null}
            loading={loading}
            selected={value === code}
            byDesign={UNRESOLVABLE_BY_DESIGN.has(code)}
            onSelect={() => onChange(code)}
          />
        ))}
      </div>

      <p className="max-w-[76ch] text-2xs leading-relaxed text-muted-foreground">
        {loading ? (
          <span role="status">Counting rows per reason code on the server…</span>
        ) : (
          <>
            All eight codes are listed whether or not this run raised any. A code
            reading{" "}
            <span className="tnum">0</span> is a code this dataset did not
            exercise — it is implemented and unit-tested, and nothing here is
            evidence that it works.{" "}
            {agrees ? (
              <span className="inline-flex items-baseline gap-1">
                <CheckIcon
                  aria-hidden
                  className="size-3 shrink-0 translate-y-0.5 text-matched"
                  strokeWidth={2}
                />
                The eight counts sum to{" "}
                <span className="tnum">{int(data?.total ?? 0)}</span>, which is
                exactly <code className="font-mono">exception_count</code>.
              </span>
            ) : data !== null ? (
              <span className="inline-flex items-baseline gap-1 text-error-fg">
                <TriangleAlertIcon
                  aria-hidden
                  className="size-3 shrink-0 translate-y-0.5"
                  strokeWidth={2}
                />
                The eight counts sum to{" "}
                <span className="tnum">{int(data.total)}</span> but{" "}
                <code className="font-mono">exception_count</code> reports{" "}
                <span className="tnum">{int(exceptionCount)}</span>. Some rows
                carry a reason code outside the contract&apos;s enum, or the
                filter is dropping rows — either way this list is incomplete, so
                do not read the totals as a census.
              </span>
            ) : null}
          </>
        )}
      </p>
    </div>
  );
}

/**
 * One filter option, carrying its own row count.
 *
 * A code with no rows is genuinely disabled: there is nothing to filter to, and
 * a control that navigates to a guaranteed-empty result is a control that lies
 * about what it does. The label and the zero stay fully legible — the count is
 * the information, and removing the option would remove it.
 */
function FilterChip({
  label,
  title,
  count,
  loading,
  selected,
  byDesign,
  onSelect,
}: {
  label: string;
  title?: string;
  count: number | null;
  loading: boolean;
  selected: boolean;
  byDesign?: boolean;
  onSelect: () => void;
}) {
  const empty = !loading && count === 0 && !selected;

  return (
    <button
      type="button"
      aria-pressed={selected}
      disabled={empty}
      title={empty ? undefined : title}
      onClick={onSelect}
      className={cn(
        "inline-flex items-baseline gap-1.5 rounded-md border px-2 py-1 text-2xs transition-colors duration-150",
        "focus-visible:focus-ring",
        selected && "border-brand/50 bg-surface-selected font-medium text-foreground",
        !selected &&
          !empty &&
          "border-border text-muted-foreground hover:bg-surface-hover hover:text-foreground active:bg-surface-active",
        // A code with no rows is not dimmed into illegibility: the count IS the
        // information here, so the disabled state is carried by a dashed border
        // and the absence of any hover response, and the text keeps its full
        // contrast.
        empty && "cursor-default border-dashed border-border text-muted-foreground",
      )}
    >
      <span className={cn(byDesign && !selected && "text-excepted-fg")}>
        {label}
      </span>
      {loading ? (
        <span aria-hidden className="skeleton inline-block h-2.5 w-5 align-middle" />
      ) : (
        <span className="tnum text-foreground">{int(count ?? 0)}</span>
      )}
      {empty ? (
        <span className="sr-only">
          — no exceptions with this reason code in this run, so there is nothing
          to filter to
        </span>
      ) : null}
    </button>
  );
}

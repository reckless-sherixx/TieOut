"use client";

import { api } from "@/lib/api";
import { useResource } from "@/lib/hooks";
import { formatTimestamp } from "@/lib/datetime";
import { RUN_STATE_LABEL } from "@/lib/labels";
import type { RunSummary } from "@/lib/types";
import { Skeleton } from "@/components/States";

const int = (n: number) => n.toLocaleString("en-IN");

/**
 * Which run this one is compared against.
 *
 * THE DEFAULT IS THE API'S DEFAULT, AND IT IS NOT COMPUTED HERE. Omitting
 * `against` makes the endpoint pick the immediately previous COMPLETED run on
 * the same dataset, which is the comparison a controller asks for without being
 * asked. The client could not reproduce that choice even if it wanted to:
 * `RunSummary` carries no `dataset_id`, so "the same dataset" is not a fact
 * this list can see. So the first option here sends no parameter at all rather
 * than guessing a run id, and it says which one the API chose once the answer
 * comes back.
 *
 * EVERY OTHER RUN IS OFFERED, INCLUDING THE ONES THAT WILL BE REFUSED. A run of
 * a different size cannot be compared — every rate has a different denominator
 * and every rupee figure a different scale — and the API answers 409. Hiding
 * those runs would make an incomparable pair look like it did not exist;
 * offering them, labelled, lets the refusal be read as the finding it is.
 */
export function BaselinePicker({
  run,
  against,
  onChange,
}: {
  run: RunSummary;
  /** The chosen baseline, or null for the API's own default. */
  against: string | null;
  onChange: (next: string | null) => void;
}) {
  const { data, error, loading } = useResource<RunSummary[]>(
    "runs:baselines",
    (signal) => api.listRuns({ signal }),
  );

  const candidates = (data ?? []).filter((r) => r.run_id !== run.run_id);

  return (
    <div className="space-y-2">
      <label
        htmlFor="drift-baseline"
        className="block text-xs font-medium"
      >
        Compare against
      </label>

      {loading && data === null ? (
        <Skeleton className="h-8 max-w-md rounded-lg" />
      ) : (
        <select
          id="drift-baseline"
          aria-describedby="drift-baseline-note"
          value={against ?? ""}
          onChange={(event) =>
            onChange(event.target.value === "" ? null : event.target.value)
          }
          // `focus:` and not `focus-visible:` on this one control. A <select>
          // does not reliably match :focus-visible in Chrome, and this element
          // clears the native outline -- so a focus-visible-only ring can leave
          // a focused control with no ring at all, which is the one thing the
          // craft floor does not permit. Everything else in the console keeps
          // focus-visible, where the pseudo-class behaves.
          className="h-8 w-full max-w-md rounded-lg border border-input bg-transparent px-2.5 text-xs transition-colors outline-none focus:focus-ring dark:bg-input/30"
        >
          <option value="">
            The previous completed run on this dataset — the API&apos;s default
          </option>
          {candidates.map((candidate) => (
            <option key={candidate.run_id} value={candidate.run_id}>
              {candidate.run_id} · seed {int(candidate.seed)} ·{" "}
              {int(candidate.record_count)} records ·{" "}
              {RUN_STATE_LABEL[candidate.state]} ·{" "}
              {formatTimestamp(candidate.created_at)}
              {candidate.record_count === run.record_count
                ? ""
                : " · different size, not comparable"}
            </option>
          ))}
        </select>
      )}

      <p
        id="drift-baseline-note"
        className="max-w-[72ch] text-2xs leading-relaxed text-muted-foreground"
      >
        {error ? (
          <>
            The run list did not load, so only the API&apos;s own default
            baseline can be chosen — which is the one the endpoint picks when no{" "}
            <code className="font-mono">against</code> is sent, and it still
            works. <span className="font-mono">{error.message}</span>
          </>
        ) : (
          <>
            This run ran {int(run.record_count)} records. A baseline of a
            different size is offered and marked, and the API refuses it with a
            409 rather than comparing rates computed over different
            denominators — which is a legible answer, and it is rendered as one.
          </>
        )}
      </p>
    </div>
  );
}

"use client";

import * as React from "react";
import { explorerApi, RECORD_SOURCES, RECORD_SOURCE_LABEL } from "@/lib/explorer";
import type { RecordSource } from "@/lib/explorer";
import { isTerminal } from "@/lib/labels";
import { cn } from "@/lib/utils";
import type { RunState } from "@/lib/types";

const int = (n: number) => n.toLocaleString("en-IN");

type Census = Record<RecordSource, number | null>;

const EMPTY: Census = { order: null, psp_txn: null, bank_line: null };

/**
 * How many rows each of the three inputs has, from the server.
 *
 * Three requests asking for a single row and reading `total` off the
 * pagination envelope. The client never holds more than one row to produce
 * them, and the counts are the reason the three options can be shown together:
 * a source reading zero is a source this dataset did not carry, which is a
 * different fact from a source whose page failed to load.
 */
function useRecordCensus(runId: string, runState: RunState) {
  const [state, setState] = React.useState<{
    key: string;
    counts: Census;
    error: Error | null;
  } | null>(null);

  const key = `${runId}:${runState}`;

  React.useEffect(() => {
    let cancelled = false;
    const controller = new AbortController();

    void (async () => {
      try {
        const pages = await Promise.all(
          RECORD_SOURCES.map((source) =>
            explorerApi.listRunRecords(
              runId,
              { source, page: 1, size: 1 },
              { signal: controller.signal },
            ),
          ),
        );
        if (cancelled) return;
        const counts = { ...EMPTY } as Census;
        RECORD_SOURCES.forEach((source, i) => {
          counts[source] = pages[i].total;
        });
        setState({ key, counts, error: null });
      } catch (cause) {
        if (cancelled) return;
        if (cause instanceof DOMException && cause.name === "AbortError") return;
        setState({
          key,
          counts: EMPTY,
          error: cause instanceof Error ? cause : new Error(String(cause)),
        });
      }
    })();

    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [key, runId]);

  const current = state?.key === key ? state : null;
  return {
    counts: current?.counts ?? EMPTY,
    error: current?.error ?? null,
    loading: current === null,
  };
}

/**
 * Which of the three input tables to page over.
 *
 * NOT A FILTER. `source` is required by the contract, and a request without it
 * is a 422 naming the three legal values rather than a 404 or an empty page —
 * deliberately, because an empty page reads as "this run has no orders" and
 * that is a different claim from "no such source". So there is no "all
 * sources" option here and there is no unselected state: one of the three is
 * always chosen, and it is always in the URL.
 */
export function SourcePicker({
  runId,
  runState,
  value,
  onChange,
}: {
  runId: string;
  runState: RunState;
  /**
   * Null when the URL names something outside the enum. NONE of the three is
   * pressed then — showing one as chosen would contradict the page, which is
   * at that moment refusing to guess which table was meant.
   */
  value: RecordSource | null;
  onChange: (next: RecordSource) => void;
}) {
  const { counts, error, loading } = useRecordCensus(runId, runState);

  return (
    <div className="space-y-3">
      <div
        role="group"
        aria-label="Which input table to page over"
        className="flex flex-wrap gap-1.5"
      >
        {RECORD_SOURCES.map((source) => (
          <SourceChip
            key={source}
            label={RECORD_SOURCE_LABEL[source]}
            count={counts[source]}
            loading={loading && error === null}
            selected={value === source}
            onSelect={() => onChange(source)}
          />
        ))}
      </div>

      <p className="max-w-[76ch] text-2xs leading-relaxed text-muted-foreground">
        {error ? (
          <>
            <span className="text-foreground">
              The row counts did not load.
            </span>{" "}
            Each is a separate request to{" "}
            <code className="font-mono">
              GET /api/runs/{runId}/records?source=…&amp;size=1
            </code>
            , and at least one failed. The table below still works; the counts
            beside each source are the part that is missing, so read a small
            page as &ldquo;did not load&rdquo; rather than as &ldquo;few
            rows&rdquo;.
          </>
        ) : loading ? (
          <span role="status">Counting rows in each input table…</span>
        ) : !isTerminal(runState) ? (
          <>
            These are the rows the engine read, so they exist from ingest
            onwards — they do not wait for the run to finish. What they carry no
            verdict about yet is whether any of them matched.
          </>
        ) : (
          <>
            One source at a time, because the three shapes are different tables
            and a page mixing them would leave a reader sniffing fields to tell
            an order from a bank line. This is the only view that shows a record
            which produced no verdict at all: everywhere else a record arrives
            attached to one.
          </>
        )}
      </p>
    </div>
  );
}

function SourceChip({
  label,
  count,
  loading,
  selected,
  onSelect,
}: {
  label: string;
  count: number | null;
  loading: boolean;
  selected: boolean;
  onSelect: () => void;
}) {
  return (
    <button
      type="button"
      aria-pressed={selected}
      onClick={onSelect}
      className={cn(
        "inline-flex items-baseline gap-1.5 rounded-md border px-2.5 py-1 text-xs transition-colors duration-150",
        "focus-visible:focus-ring",
        selected
          ? "border-brand/50 bg-surface-selected font-medium text-foreground"
          : "border-border text-muted-foreground hover:bg-surface-hover hover:text-foreground active:bg-surface-active",
      )}
    >
      {label}
      {loading ? (
        <span
          aria-hidden
          className="skeleton inline-block h-2.5 w-6 align-middle"
        />
      ) : count === null ? null : (
        <span className="tnum text-foreground">{int(count)}</span>
      )}
    </button>
  );
}

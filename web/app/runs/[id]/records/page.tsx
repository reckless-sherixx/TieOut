"use client";

import * as React from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { isRecordSource, RECORD_SOURCE_LABEL } from "@/lib/explorer";
import type { RecordSource } from "@/lib/explorer";
import { useRun } from "@/components/shell/RunScope";
import { ViewIntro } from "@/components/shell/ViewIntro";
import { EmptyState, LoadingBlock } from "@/components/States";
import { Button } from "@/components/ui/button";
import { RecordTable } from "@/components/records/RecordTable";
import { SourcePicker } from "@/components/records/SourcePicker";

/**
 * The ingested source rows: orders, PSP transactions, bank lines.
 *
 * `source` IS REQUIRED AND THIS PAGE ALWAYS SENDS IT. A request without it is
 * a 422 naming the three legal values — not a 404 and not an empty page — and
 * that refusal is deliberate: an empty page reads as "this run has no orders",
 * which is a completely different claim from "you did not say which table you
 * meant". So there is no unselected state here, no "all sources" option, and
 * the chosen source is written into the URL rather than held in memory, which
 * is also what makes a link to this view mean one specific thing.
 *
 * A URL that names something outside the enum is not silently corrected to
 * orders. Guessing would produce a page of real rows under a heading the
 * reader did not ask for, which is the same failure the 422 exists to prevent.
 *
 * Paging is server-side, so the browser holds fifty rows whether the run
 * ingested fifty or five thousand.
 */
export default function RecordsPage() {
  return (
    <div className="space-y-14">
      <ViewIntro
        title="Records"
        lede="The rows the engine actually read, before any verdict was formed about them. This is the only view that shows a record which produced neither a match nor an exception — everywhere else a record arrives attached to one, so a row that matched cleanly has no other way to reach the screen. Narration is rendered verbatim, double spaces and all: the garbling is the data, and normalising it for display would erase the defect."
      />

      {/* useSearchParams renders its subtree on the client. The boundary keeps
          the run header, the view rail and this heading out of that. */}
      <React.Suspense
        fallback={
          <LoadingBlock
            label="Reading the source, page and open record from the URL"
            lines={3}
            className="max-w-xl"
          />
        }
      >
        <RecordsView />
      </React.Suspense>
    </div>
  );
}

const DEFAULT_SOURCE: RecordSource = "order";

function RecordsView() {
  const run = useRun();
  const router = useRouter();
  const pathname = usePathname();
  const params = useSearchParams();

  const sourceParam = params.get("source");
  const pageParam = Number(params.get("page"));
  const page = Number.isInteger(pageParam) && pageParam >= 1 ? pageParam : 1;
  const openId = params.get("rec");

  const setParams = React.useCallback(
    (next: Record<string, string | null>) => {
      const search = new URLSearchParams(params.toString());
      for (const [key, value] of Object.entries(next)) {
        if (value === null) search.delete(key);
        else search.set(key, value);
      }
      const query = search.toString();
      router.replace(query ? `${pathname}?${query}` : pathname, {
        scroll: false,
      });
    },
    [params, pathname, router],
  );

  // A URL with no source at all is the one case worth completing rather than
  // refusing: nothing was named, so nothing can be misread. It is written into
  // the address bar rather than defaulted in memory, so that what the page
  // requested and what the URL says are the same thing.
  React.useEffect(() => {
    if (sourceParam === null) setParams({ source: DEFAULT_SOURCE });
  }, [sourceParam, setParams]);

  /**
   * Three states, not two, and the middle one is why this page used to flash
   * an error on a perfectly good navigation.
   *
   *   a source in the enum      -> render it
   *   NO source in the URL      -> the effect above is writing one; wait
   *   a source outside the enum -> refuse, and say what was named
   *
   * Folding the middle case into the third rendered "This URL names a source
   * that does not exist — `source=` is not one of the three" for one frame on
   * every hard navigation to `/records`, interpolating an empty string into
   * the sentence. Nothing had been named, so nothing could be wrong with it.
   */
  const source = isRecordSource(sourceParam) ? sourceParam : null;
  const refused = sourceParam !== null && source === null;

  return (
    <section aria-labelledby="records-heading" className="space-y-6">
      <div className="space-y-2">
        <h2 id="records-heading" className="text-base font-medium tracking-tight">
          One input table at a time
        </h2>
        <p className="max-w-[72ch] text-xs leading-relaxed text-muted-foreground">
          The API requires a source and refuses a request without one, which is
          why this is a choice of three rather than a filter with an
          &ldquo;all&rdquo; option. Rows are ordered by each source&apos;s own id
          field, so paging is stable and no row appears twice. Open a row for
          every field as ingested — including the ones the table has no column
          for, and the full narration rather than the clipped one.
        </p>
      </div>

      <SourcePicker
        runId={run.run_id}
        runState={run.state}
        value={source}
        onChange={(next) =>
          // A different source is a different row set, so page 1 is the only
          // safe page and the open record cannot survive the switch.
          setParams({ source: next, page: null, rec: null })
        }
      />

      {source === null && !refused ? (
        // The effect above is writing `?source=order` into the address bar.
        // A skeleton of the table that is one frame away, not an error and not
        // a blank: the shape of what is coming is already known.
        <LoadingBlock
          label="Completing the URL with the default source"
          lines={3}
          className="max-w-xl"
        />
      ) : source === null ? (
        <EmptyState
          title="This URL names a source that does not exist"
          reason={
            <>
              <code className="font-mono">source={sourceParam}</code> is not one
              of the three the contract accepts —{" "}
              <code className="font-mono">order</code>,{" "}
              <code className="font-mono">psp_txn</code>,{" "}
              <code className="font-mono">bank_line</code>. Nothing was
              requested, deliberately: showing orders instead would put real
              rows under a heading you did not ask for, and an empty table would
              read as &ldquo;this run has no records&rdquo;. Pick one of the
              three above, or take the button below.
            </>
          }
          action={
            <Button
              variant="outline"
              size="sm"
              onClick={() =>
                setParams({ source: DEFAULT_SOURCE, page: null, rec: null })
              }
            >
              Show {RECORD_SOURCE_LABEL[DEFAULT_SOURCE].toLowerCase()}
            </Button>
          }
        />
      ) : (
        <RecordTable
          runId={run.run_id}
          runState={run.state}
          source={source}
          page={page}
          openId={openId}
          onPageChange={(next) =>
            setParams({ page: next === 1 ? null : String(next), rec: null })
          }
          onOpen={(recordId) => setParams({ rec: recordId })}
          onClose={() => setParams({ rec: null })}
        />
      )}
    </section>
  );
}

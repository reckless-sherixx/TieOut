"use client";

import * as React from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useRun } from "@/components/shell/RunScope";
import { ViewIntro } from "@/components/shell/ViewIntro";
import { LoadingBlock } from "@/components/States";
import { ExceptionTable } from "@/components/exceptions/ExceptionTable";
import { ReasonFilter } from "@/components/exceptions/ReasonFilter";
import { AuditPanel } from "@/components/exceptions/AuditPanel";
import { REASON_CODES } from "@/lib/labels";
import type { ReasonCode, ReconExceptionDetail } from "@/lib/types";

/**
 * The itemised exception list.
 *
 * Every subject the engine did not match, each with a machine-readable reason
 * code, and each one interaction from its own audit trail. Rejected hypotheses
 * are never filtered out and never collapsed: a rejection is the visible
 * evidence that the verifier fires, and hiding it would leave only the outcomes
 * that flatter the system.
 *
 * THE FILTER, THE PAGE AND THE OPEN ROW ALL LIVE IN THE URL. A reviewer working
 * an exception list needs to hand someone the exact thing they are looking at —
 * "the amount mismatches on page 3", "this specific exception" — and a URL that
 * says so does that where a URL plus a remembered sequence of clicks does not.
 * It is the same argument that made the six views of a run real routes.
 */
export default function ExceptionsPage() {
  return (
    <div className="space-y-14">
      <ViewIntro
        title="Exceptions"
        lede="Every subject the engine declined, with the typed reason it declined it. Two of the eight reason codes are the designed outcome rather than a failure: a subject the data does not determine is supposed to end up here, and declining it is exactly what the trap-capture rate measures. Filtering and paging both happen on the server, so this page holds fifty rows whether the run raised fifty or five thousand."
      />

      {/* useSearchParams renders its subtree on the client. The boundary keeps
          everything above it — the run header, the view rail, this heading —
          out of that, and gives the reader the shape of what is coming rather
          than a blank frame. */}
      <React.Suspense
        fallback={
          <LoadingBlock
            label="Reading the filter and page from the URL"
            lines={3}
            className="max-w-xl"
          />
        }
      >
        <ExceptionsView />
      </React.Suspense>
    </div>
  );
}

function ExceptionsView() {
  const run = useRun();
  const router = useRouter();
  const pathname = usePathname();
  const params = useSearchParams();

  const [seed, setSeed] = React.useState<ReconExceptionDetail | null>(null);

  const reasonParam = params.get("reason");
  const reasonCode: ReasonCode | null =
    reasonParam !== null && (REASON_CODES as readonly string[]).includes(reasonParam)
      ? (reasonParam as ReasonCode)
      : null;

  const pageParam = Number(params.get("page"));
  const page =
    Number.isInteger(pageParam) && pageParam >= 1 ? pageParam : 1;

  const openId = params.get("exc");

  /**
   * One writer for the query string. `replace` rather than `push`: a filter and
   * a page are view state, and forty history entries for forty pages turns the
   * back button into an undo stack nobody asked for. The URL still carries the
   * state, so it is still shareable.
   */
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

  return (
    <>
      <section aria-labelledby="list-heading" className="space-y-6">
        <div className="space-y-2">
          <h2 id="list-heading" className="text-base font-medium tracking-tight">
            Every subject the engine declined
          </h2>
          <p className="max-w-[72ch] text-xs leading-relaxed text-muted-foreground">
            Ordering is stable on{" "}
            <code className="font-mono">exception_id</code>, which is what makes
            paging safe: no row can appear on two pages, and there is no
            client-side de-duplication here because de-duplication would hide an
            ordering bug rather than reveal one. Open a row for the subject
            record, the hypothesis if one was made, the verdict, and the specific
            check that refused it.
          </p>
        </div>

        <ReasonFilter
          runId={run.run_id}
          runState={run.state}
          exceptionCount={run.exception_count}
          value={reasonCode}
          onChange={(next) =>
            // A filter changes the row set, so page 1 is the only safe page.
            setParams({ reason: next, page: null })
          }
        />

        <ExceptionTable
          runId={run.run_id}
          runState={run.state}
          reasonCode={reasonCode}
          page={page}
          openId={openId}
          onPageChange={(next) =>
            setParams({ page: next === 1 ? null : String(next) })
          }
          onClearFilter={() => setParams({ reason: null, page: null })}
          onOpen={(row) => {
            setSeed(row);
            setParams({ exc: row.exception_id });
          }}
        />
      </section>

      <AuditPanel
        exceptionId={openId}
        seed={seed}
        onClose={() => setParams({ exc: null })}
      />
    </>
  );
}

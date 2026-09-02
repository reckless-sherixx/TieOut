"use client";

import * as React from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useRun } from "@/components/shell/RunScope";
import { ViewIntro } from "@/components/shell/ViewIntro";
import { LoadingBlock } from "@/components/States";
import { SettlementTable } from "@/components/settlements/SettlementTable";

/**
 * Every settlement of a run, browsable.
 *
 * This is the view that turns a hand-picked demo into something a reviewer can
 * audit. Before it, a settlement could only be reached if you already knew its
 * id, and a batch that never closed could not be reached at all — so the screen
 * could only ever show a batch that worked.
 *
 * TWO RULES GOVERN EVERY NUMBER ON IT, AND BOTH ARE ABOUT NOT LYING.
 *
 * 1. The engine's number wins. Every money field is rendered as the API sent
 *    it. Re-summing the raw PSP legs in the browser would be a second
 *    reconstruction that disagrees with the first: the matcher suppresses
 *    duplicate payment legs before it reconstructs, and re-summing overstates
 *    gross on nine batches of the reference dataset.
 *
 * 2. The residual is rendered, never absorbed. `net` is the bank credit, not
 *    the sum of the columns beside it, and at tier T3 the two differ within
 *    tolerance by design. A table that quietly balanced itself would describe a
 *    match that never happened.
 *
 * THE PAGE AND THE OPEN ROW LIVE IN THE URL, for the same reason the six views
 * of a run are real routes: a reviewer needs to hand someone the exact thing
 * they are looking at.
 */
export default function SettlementsPage() {
  return (
    <div className="space-y-14">
      <ViewIntro
        title="Settlements"
        lede="One row per settlement the run's PSP legs name — closed against a bank line or not — carrying the breakdown the engine reconstructed, the payment legs it counted, and the tier that closed it. Paging happens on the server, so this page holds fifty rows whether the run produced a hundred settlements or five thousand."
      />

      {/* useSearchParams renders its subtree on the client. The boundary keeps
          the run header, the view rail and this heading out of that, and gives
          the reader the shape of what is coming rather than a blank frame. */}
      <React.Suspense
        fallback={
          <LoadingBlock
            label="Reading the page and the open settlement from the URL"
            lines={3}
            className="max-w-xl"
          />
        }
      >
        <SettlementsView />
      </React.Suspense>
    </div>
  );
}

function SettlementsView() {
  const run = useRun();
  const router = useRouter();
  const pathname = usePathname();
  const params = useSearchParams();

  const pageParam = Number(params.get("page"));
  const page = Number.isInteger(pageParam) && pageParam >= 1 ? pageParam : 1;
  const openId = params.get("setl");

  /**
   * One writer for the query string. `replace` rather than `push`: a page
   * number and an open row are view state, and forty history entries for forty
   * pages turn the back button into an undo stack nobody asked for. The URL
   * still carries the state, so it is still shareable.
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
    <section aria-labelledby="settlements-heading" className="space-y-6">
      <div className="space-y-2">
        <h2
          id="settlements-heading"
          className="text-base font-medium tracking-tight"
        >
          Every settlement the run named, closed or not
        </h2>
        <p className="max-w-[72ch] text-xs leading-relaxed text-muted-foreground">
          Ordering is stable on{" "}
          <code className="font-mono">settlement_id</code>, which is what
          makes paging safe: no row appears on two pages, and there is no
          client-side de-duplication because de-duplication would hide an
          ordering bug rather than reveal one. A matched row carries the
          match&apos;s own fields; an unmatched row carries the matcher&apos;s
          reconstruction over the batch&apos;s active legs. Neither is summed
          a second time here. Open a row for the arithmetic, the residual, the
          engine&apos;s own account of why it closed — and, where it closed,
          the fan of orders into the batch drawn. The listing carries no order
          lines because the settlements endpoint does not serve them, so the
          drawing is fetched for the one row you open.
        </p>
      </div>

      <SettlementTable
        runId={run.run_id}
        runState={run.state}
        page={page}
        openId={openId}
        onPageChange={(next) =>
          // The open row lives on the page it was opened from, so paging away
          // from it closes it rather than leaving a stale panel under a table
          // that no longer contains its row.
          setParams({ page: next === 1 ? null : String(next), setl: null })
        }
        onOpen={(settlementId) => setParams({ setl: settlementId })}
        onClose={() => setParams({ setl: null })}
      />
    </section>
  );
}

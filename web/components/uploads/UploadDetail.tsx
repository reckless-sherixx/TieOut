"use client";

import * as React from "react";
import { XIcon } from "lucide-react";
import { useResource } from "@/lib/hooks";
import {
  QUARANTINE_REASON,
  UPLOAD_STATE,
  formatBytes,
  formatLabel,
  uploadsApi,
  type PaginatedQuarantine,
  type Upload,
} from "@/lib/uploads";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Panel, PanelHeader } from "@/components/Panel";
import { ErrorState } from "@/components/States";
import {
  PAGE_SIZE,
  PageBar,
  PastTheEnd,
  TableSkeleton,
  Th,
  isPastTheEnd,
  pageRange,
  useHeldPage,
} from "@/components/explorer/paging";

const int = (n: number) => n.toLocaleString("en-IN");

const SKELETON_COLUMNS = ["3rem", "10rem", "22rem"];

/**
 * One upload, opened: what it was read as, what came out, and what did not.
 *
 * THE QUARANTINE TABLE IS THE REASON THIS PANEL EXISTS. Every row a real bank
 * export refuses is kept verbatim with the line number an editor would show,
 * and this is the only surface in the whole product that renders a merchant's
 * own file content back to them — which is why it is behind the same session
 * every other financial read is behind, and why no error message anywhere else
 * quotes a row.
 *
 * The three states are three different empty tables and they say different
 * things. A file that read cleanly has nothing to review; a file whose every
 * row was refused has everything to review; a file with no data rows has
 * nothing to review AND nothing to run, and telling the last two apart is what
 * stops a merchant hunting for damage in a file that simply had no rows.
 */
export function UploadDetail({
  upload,
  page,
  onPageChange,
  onClose,
}: {
  upload: Upload;
  page: number;
  onPageChange: (next: number) => void;
  onClose: () => void;
}) {
  const panelRef = React.useRef<HTMLElement | null>(null);

  /**
   * The panel takes focus when it opens, the same contract the settlement
   * breakdown honours: it renders below a table, and on a row past the first
   * few the only feedback a click produced would be a changed address bar.
   * Programmatically focusable and out of the tab order, because it is a
   * region a control sends you to rather than one you tab into.
   */
  React.useEffect(() => {
    const panel = panelRef.current;
    if (!panel) return;
    panel.scrollIntoView({ block: "start", behavior: "auto" });
    panel.focus({ preventScroll: true });
  }, [upload.upload_id]);

  const close = React.useCallback(() => {
    const trigger = document.querySelector<HTMLElement>(
      `[data-upload-trigger="${CSS.escape(upload.upload_id)}"]`,
    );
    onClose();
    trigger?.focus();
  }, [onClose, upload.upload_id]);

  const state = UPLOAD_STATE[upload.state];

  return (
    <Panel
      ref={panelRef}
      tabIndex={-1}
      aria-labelledby="upload-detail-heading"
      className="scroll-mt-4 focus-visible:focus-ring"
    >
      <PanelHeader
        title={
          <span
            id="upload-detail-heading"
            className="flex flex-wrap items-baseline gap-x-3 gap-y-1"
          >
            <span className="break-all">{upload.filename}</span>
            <span className="text-2xs font-normal text-muted-foreground">
              read as{" "}
              <span className="text-foreground">
                {formatLabel(upload.format_id)}
              </span>{" "}
              <span className="tnum">
                ({(upload.confidence * 100).toFixed(0)}% confidence)
              </span>
            </span>
          </span>
        }
        description={state.note}
        action={
          <Button
            variant="ghost"
            size="icon-sm"
            aria-label={`Close the review of ${upload.filename}`}
            onClick={close}
          >
            <XIcon aria-hidden strokeWidth={2} />
          </Button>
        }
      />

      <div className="space-y-8 border-t border-border px-6 py-6">
        <Facts upload={upload} />
        <Quarantine upload={upload} page={page} onPageChange={onPageChange} />
      </div>
    </Panel>
  );
}

/* ------------------------------------------------------------------ *
 * What the file is
 * ------------------------------------------------------------------ */

function Facts({ upload }: { upload: Upload }) {
  const rows: [string, React.ReactNode][] = [
    [
      "Upload",
      <span key="id" className="font-mono break-all">
        {upload.upload_id}
      </span>,
    ],
    [
      "Content hash",
      <span key="hash" className="font-mono text-2xs break-all">
        {upload.content_sha256}
      </span>,
    ],
    ["Size", <span key="size" className="tnum">{formatBytes(upload.byte_size)}</span>],
    [
      "Encoding",
      upload.encoding ? (
        <span key="enc" className="font-mono">
          {upload.encoding}
        </span>
      ) : (
        <span key="enc" className="text-muted-foreground">
          none — nothing decoded this file
        </span>
      ),
    ],
    [
      "Format version",
      <span key="ver" className="font-mono">
        {upload.format_version || "—"}
      </span>,
    ],
    [
      "Records",
      <span key="rec" className="tnum">
        {int(upload.order_count)} orders · {int(upload.psp_txn_count)} PSP legs ·{" "}
        {int(upload.bank_line_count)} bank lines
      </span>,
    ],
    [
      "Rows skipped",
      upload.skipped_rows === 0 ? (
        <span key="skip" className="text-muted-foreground">
          none
        </span>
      ) : (
        <span key="skip" className="tnum">
          {int(upload.skipped_rows)} — trailing blocks the layout ignores
        </span>
      ),
    ],
  ];

  return (
    <div className="space-y-4">
      <dl className="grid grid-cols-[8.5rem_minmax(0,1fr)] gap-x-4 gap-y-2.5 text-xs">
        {rows.map(([label, value]) => (
          <React.Fragment key={label}>
            <dt className="text-muted-foreground">{label}</dt>
            <dd className="min-w-0">{value}</dd>
          </React.Fragment>
        ))}
      </dl>

      {/* The arithmetic that has to close, stated where a reviewer can check
          it. `records + quarantined + skipped` is every data row the file
          carried; a layer that silently dropped one would break this sum, and
          that is the whole reason `skipped_rows` is on the wire at all. */}
      <p className="tnum max-w-prose text-2xs leading-relaxed text-muted-foreground">
        {int(upload.record_count)} records + {int(upload.quarantine_count)}{" "}
        quarantined + {int(upload.skipped_rows)} skipped ={" "}
        {int(
          upload.record_count + upload.quarantine_count + upload.skipped_rows,
        )}{" "}
        data rows accounted for. Nothing is dropped silently — that sum is the
        guarantee, not a summary.
      </p>
    </div>
  );
}

/* ------------------------------------------------------------------ *
 * The rows that did not make it
 * ------------------------------------------------------------------ */

function Quarantine({
  upload,
  page,
  onPageChange,
}: {
  upload: Upload;
  page: number;
  onPageChange: (next: number) => void;
}) {
  const enabled = upload.quarantine_count > 0;
  const { data, error, loading, refresh } = useResource<PaginatedQuarantine>(
    `quarantine:${upload.upload_id}:${page}`,
    (signal) =>
      uploadsApi.listQuarantine(
        upload.upload_id,
        { page, size: PAGE_SIZE },
        { signal },
      ),
    enabled,
  );

  const { shown, stale } = useHeldPage(upload.upload_id, data, loading);
  const rows = shown?.items ?? [];
  const range = pageRange(page, shown?.total ?? 0);

  if (!enabled) {
    return (
      <section aria-labelledby="quarantine-heading" className="space-y-2">
        <h3 id="quarantine-heading" className="text-xs font-medium">
          Nothing was quarantined
        </h3>
        <p className="max-w-prose text-xs leading-relaxed text-muted-foreground">
          Every row of this file became a canonical record. A malformed row
          would appear here with its raw text and line number; there are none.
        </p>
      </section>
    );
  }

  // An accepted file that produced nothing is now ONE quarantine row carrying
  // EMPTY_DOCUMENT, rather than an `empty` upload state -- the state collapsed
  // when the ingest boundary started refusing to return silently. The
  // distinction the old state carried still matters to a merchant, so it is
  // read off the reason code here instead: "this file had nothing in it" and
  // "this file was full of problems" are different things to be told.
  const emptyDocument =
    upload.quarantine_count === 1 &&
    upload.record_count === 0 &&
    rows.length === 1 &&
    rows[0]?.reason === "EMPTY_DOCUMENT";

  if (emptyDocument) {
    return (
      <section aria-labelledby="quarantine-heading" className="space-y-2">
        <h3 id="quarantine-heading" className="text-xs font-medium">
          This file had nothing in it
        </h3>
        <p className="max-w-prose text-xs leading-relaxed text-muted-foreground">
          The format was recognised and no transaction rows followed, so there
          was nothing to refuse and nothing to reconcile. That is a file with
          nothing in it rather than a file full of problems — check the date
          range on the export. It is reported rather than passed over, because
          an ingest that succeeded silently is indistinguishable from one that
          worked.
        </p>
      </section>
    );
  }

  return (
    <section aria-labelledby="quarantine-heading" className="space-y-4">
      <div className="space-y-1.5">
        <h3 id="quarantine-heading" className="text-xs font-medium">
          {int(upload.quarantine_count)}{" "}
          {upload.quarantine_count === 1 ? "row" : "rows"} could not be read
        </h3>
        <p className="max-w-prose text-xs leading-relaxed text-muted-foreground">
          Kept exactly as they arrived, with the line number an editor shows.
          {upload.record_count > 0 ? (
            <>
              {" "}
              The other{" "}
              <span className="tnum">{int(upload.record_count)}</span> rows were
              read and this file can still feed a run — damage costs the rows it
              is on, not the file.
            </>
          ) : (
            <>
              {" "}
              Every row of the file is here, so nothing came out of it and it
              cannot feed a run.
            </>
          )}
        </p>
      </div>

      {error ? (
        <ErrorState
          title="The quarantined rows did not load"
          error={error}
          recovery={
            <>
              <code className="font-mono">
                GET /api/uploads/{upload.upload_id}/quarantine?page={page}
              </code>{" "}
              failed. The counts above came from the uploads listing and are
              unaffected.
            </>
          }
          onRetry={refresh}
        />
      ) : loading && shown === null ? (
        <TableSkeleton
          label="Loading the quarantined rows"
          columns={SKELETON_COLUMNS}
        />
      ) : isPastTheEnd(range, rows.length) ? (
        <PastTheEnd
          range={range}
          unit="quarantined rows"
          onPageChange={onPageChange}
        />
      ) : (
        <div className="space-y-4">
          <PageBar
            range={range}
            unit="quarantined rows"
            busy={stale}
            onPageChange={onPageChange}
          />

          <div
            aria-busy={stale}
            className={cn(
              "relative overflow-x-auto rounded-xl border border-border transition-opacity duration-150",
              stale && "opacity-60",
            )}
          >
            {stale ? (
              <span className="sr-only" role="status">
                Loading page {page}
              </span>
            ) : null}

            <table className="w-full min-w-[52rem] border-collapse text-left">
              <caption className="sr-only">
                Quarantined rows {range.from} to {range.to} of {range.total},
                ordered by line number. Each carries the raw text exactly as it
                arrived and the reason it could not become a record.
              </caption>
              <thead>
                <tr className="border-b border-border bg-surface">
                  <Th className="w-16 pl-4 text-right">Line</Th>
                  <Th className="w-64">Why</Th>
                  <Th className="pr-4">The row, as it arrived</Th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <tr
                    key={`${row.row_number}-${row.raw}`}
                    className="border-b border-border last:border-b-0"
                  >
                    <th
                      scope="row"
                      className="tnum py-3 pr-4 pl-4 text-right align-top text-xs font-normal text-muted-foreground"
                    >
                      {int(row.row_number)}
                    </th>
                    <td className="py-3 pr-4 align-top text-xs">
                      <span className="block text-excepted-fg">
                        {QUARANTINE_REASON[row.reason]}
                      </span>
                      <span className="mt-0.5 block text-2xs leading-relaxed text-muted-foreground">
                        {row.detail}
                      </span>
                      <span className="mt-0.5 block font-mono text-2xs text-muted-foreground">
                        {row.reason}
                      </span>
                    </td>
                    <td className="py-3 pr-4 align-top">
                      {row.raw ? (
                        <code className="block max-w-[42rem] font-mono text-2xs leading-relaxed break-all whitespace-pre-wrap text-foreground">
                          {row.raw}
                        </code>
                      ) : (
                        <span className="text-2xs text-muted-foreground">
                          No row text — this is a fact about the file rather
                          than about one of its lines.
                        </span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </section>
  );
}

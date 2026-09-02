"use client";

import * as React from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useResource } from "@/lib/hooks";
import { uploadsApi, type Upload } from "@/lib/uploads";
import { EmptyState, ErrorState, LoadingBlock } from "@/components/States";
import { Panel, PanelHeader } from "@/components/Panel";
import { UploadDropzone } from "@/components/uploads/UploadDropzone";
import { UploadList } from "@/components/uploads/UploadList";
import { UploadDetail } from "@/components/uploads/UploadDetail";
import { RunFromUploads } from "@/components/uploads/RunFromUploads";

/**
 * Your files: upload, see what was read, review what was not, reconcile.
 *
 * This is the front door of the product. Everything else in this console
 * operates on a dataset the system generated for itself; this operates on a
 * file a merchant exported from Razorpay, from their bank, or from Shopify —
 * and the run it starts goes through the same engine, which is the whole
 * claim.
 *
 * THE OPEN FILE AND ITS QUARANTINE PAGE LIVE IN THE URL, for the same reason
 * the settlement breakdown does: a reviewer looking at row 412 of a damaged
 * bank export needs to be able to hand someone that exact screen.
 *
 * THE SELECTION DOES NOT live in the URL. It is a transient choice on the way
 * to a run, and a URL that carried six upload ids would be a link that means
 * something different the moment one of them is re-uploaded. The run it
 * produces is the shareable artefact, and that has its own id.
 */
export default function UploadsPage() {
  return (
    <div className="mx-auto w-full max-w-[92rem] space-y-10 px-6 py-12 lg:px-8">
      <div className="max-w-[72ch] space-y-2">
        <h1 className="text-xl font-medium tracking-tight">Your files</h1>
        <p className="text-sm leading-relaxed text-muted-foreground">
          A Razorpay settlement report, a bank statement, an order export, a COD
          remittance. Each one is read by its header rather than by its name,
          turned into the same canonical records the engine already reconciles,
          and kept by content — the same file uploaded twice is the same file,
          not two.
        </p>
      </div>

      {/* useSearchParams renders its subtree on the client. The boundary keeps
          the heading above out of that and gives the reader the shape of what
          is coming rather than a blank page. */}
      <React.Suspense
        fallback={
          <LoadingBlock
            label="Reading the open file from the URL"
            lines={3}
            className="max-w-xl"
          />
        }
      >
        <UploadsView />
      </React.Suspense>
    </div>
  );
}

function UploadsView() {
  const router = useRouter();
  const pathname = usePathname();
  const params = useSearchParams();

  const openId = params.get("upload");
  const pageParam = Number(params.get("qpage"));
  const quarantinePage =
    Number.isInteger(pageParam) && pageParam >= 1 ? pageParam : 1;

  const [selected, setSelected] = React.useState<ReadonlySet<string>>(
    () => new Set(),
  );

  const { data, error, loading, refresh } = useResource<Upload[]>(
    "uploads",
    (signal) => uploadsApi.listUploads({ signal }),
  );

  /** One writer for the query string. `replace`, so paging is not history. */
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

  // Memoised because `selectAll` closes over it: `data ?? []` builds a new
  // array every render, which would rebuild the callback every render, which
  // would make the checkbox in the table header a new prop every render.
  const uploads = React.useMemo(() => data ?? [], [data]);
  const open = uploads.find((upload) => upload.upload_id === openId) ?? null;
  const chosen = uploads.filter((upload) => selected.has(upload.upload_id));

  const toggle = React.useCallback((uploadId: string) => {
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(uploadId)) next.delete(uploadId);
      else next.add(uploadId);
      return next;
    });
  }, []);

  const selectAll = React.useCallback(
    (next: boolean) => {
      setSelected(
        next
          ? new Set(
              uploads
                .filter((upload) => upload.record_count > 0)
                .map((upload) => upload.upload_id),
            )
          : new Set(),
      );
    },
    [uploads],
  );

  return (
    <div className="space-y-10">
      <UploadDropzone onIngested={refresh} />

      <Panel className="overflow-hidden">
        <PanelHeader
          title="Uploaded"
          description="Newest first. A file that produced records can feed a run; one that produced none is listed with what stopped it, because an empty export and an export full of damage are different problems with different fixes."
        />

        <div className="border-t border-border">
          {error ? (
            <div className="p-6">
              <ErrorState
                title="Your files did not load"
                error={error}
                recovery={
                  <>
                    <code className="font-mono">GET /api/uploads</code> failed.
                    Nothing you have uploaded is affected — this is the listing,
                    not the store.
                  </>
                }
                onRetry={refresh}
              />
            </div>
          ) : loading && data === null ? (
            <div className="p-6">
              <LoadingBlock label="Loading your files" lines={4} />
            </div>
          ) : uploads.length === 0 ? (
            <div className="p-6">
              <EmptyState
                title="No files yet"
                reason={
                  <>
                    Drop a settlement report or a bank statement above. Nothing
                    is inferred from the filename: the first line of the file
                    decides what it is, so an export renamed{" "}
                    <code className="font-mono">final_FINAL.csv</code> is read
                    as whatever it actually contains.
                  </>
                }
              />
            </div>
          ) : (
            <UploadList
              uploads={uploads}
              selected={selected}
              openId={openId}
              onToggle={toggle}
              onSelectAll={selectAll}
              onOpen={(uploadId) =>
                setParams({
                  upload: uploadId === openId ? null : uploadId,
                  // A different file's quarantine starts at page 1; carrying
                  // page 7 across would open a review past the end of a file
                  // that may have three bad rows.
                  qpage: null,
                })
              }
            />
          )}
        </div>
      </Panel>

      {chosen.length > 0 ? (
        <RunFromUploads
          uploads={chosen}
          onStarted={() => setSelected(new Set())}
        />
      ) : uploads.some((upload) => upload.record_count > 0) ? (
        <p className="max-w-prose text-xs leading-relaxed text-muted-foreground">
          Select the files a run should read — a settlement report, a bank
          statement and an order export together reconcile the whole chain, and
          any subset of them is a valid run that reports less.
        </p>
      ) : null}

      {open ? (
        <UploadDetail
          upload={open}
          page={quarantinePage}
          onPageChange={(next) =>
            setParams({ qpage: next === 1 ? null : String(next) })
          }
          onClose={() => setParams({ upload: null, qpage: null })}
        />
      ) : null}
    </div>
  );
}

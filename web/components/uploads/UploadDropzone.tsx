"use client";

import * as React from "react";
import { CheckIcon, FileUpIcon, RotateCcwIcon, XIcon } from "lucide-react";
import { ApiError, ApiNetworkError } from "@/lib/api";
import {
  formatBytes,
  formatLabel,
  refusalOf,
  uploadsApi,
  type UploadReceipt,
  type UploadRefusedError,
} from "@/lib/uploads";
import { cn } from "@/lib/utils";

/**
 * Drop a file, or pick one. One request per file, in order.
 *
 * SEQUENTIAL AND NOT PARALLEL, on purpose. Dropping a settlement report, a
 * bank statement and an order export at once is the normal case, and firing
 * three multipart POSTs together against SQLite buys nothing a merchant can
 * perceive while making the results arrive in an order nobody chose. In
 * sequence, the list below reads in the order the files were dropped.
 *
 * THE THREE OUTCOMES ARE THREE DIFFERENT SENTENCES.
 *
 * * **Read** — the file became canonical records. It says which format it was
 *   detected as and with what confidence, because detection is by header shape
 *   and a merchant is entitled to see that the guess was not a guess.
 * * **Already held** — the same bytes were uploaded before. This is
 *   RECOGNITION, not an error: it is rendered in the ordinary state colour with
 *   the id of the upload that already exists, because "we have this" is a
 *   useful answer and a red banner would say the opposite.
 * * **Refused** — no adapter recognised it. The candidate scores and the
 *   threshold are shown, so "why did it not read my export" is answered on
 *   screen rather than in a support ticket.
 *
 * The dropzone is a real `<button>` wrapping a visually hidden `<input
 * type="file">`, so the whole affordance is reachable and operable from the
 * keyboard. A `<div>` with an onClick would be a drop target that a keyboard
 * user cannot use at all.
 */

type Outcome =
  | { kind: "pending"; name: string }
  | { kind: "read"; name: string; receipt: UploadReceipt }
  | { kind: "held"; name: string; receipt: UploadReceipt }
  | { kind: "refused"; name: string; refusal: UploadRefusedError }
  | { kind: "failed"; name: string; message: string };

export function UploadDropzone({
  onIngested,
}: {
  /** Called once after a batch settles, so the listing re-reads. */
  onIngested: () => void;
}) {
  const inputRef = React.useRef<HTMLInputElement | null>(null);
  const [over, setOver] = React.useState(false);
  const [busy, setBusy] = React.useState(false);
  const [outcomes, setOutcomes] = React.useState<Outcome[]>([]);

  const send = React.useCallback(
    async (files: File[]) => {
      if (files.length === 0 || busy) return;
      setBusy(true);
      setOutcomes(files.map((file) => ({ kind: "pending", name: file.name })));

      for (const [index, file] of files.entries()) {
        let outcome: Outcome;
        try {
          const receipt = await uploadsApi.createUpload(file);
          outcome = {
            kind: receipt.already_ingested ? "held" : "read",
            name: file.name,
            receipt,
          };
        } catch (cause) {
          const refusal = refusalOf(cause);
          if (refusal) {
            outcome = { kind: "refused", name: file.name, refusal };
          } else if (cause instanceof ApiNetworkError) {
            outcome = {
              kind: "failed",
              name: file.name,
              message: `${cause.message}. Nothing was uploaded.`,
            };
          } else if (cause instanceof ApiError) {
            outcome = {
              kind: "failed",
              name: file.name,
              message: cause.detail ?? `The API answered ${cause.status}.`,
            };
          } else {
            outcome = {
              kind: "failed",
              name: file.name,
              message: cause instanceof Error ? cause.message : String(cause),
            };
          }
        }
        setOutcomes((current) =>
          current.map((entry, i) => (i === index ? outcome : entry)),
        );
      }

      setBusy(false);
      onIngested();
    },
    [busy, onIngested],
  );

  return (
    <div className="space-y-4">
      <div
        onDragOver={(event) => {
          event.preventDefault();
          setOver(true);
        }}
        onDragLeave={() => setOver(false)}
        onDrop={(event) => {
          event.preventDefault();
          setOver(false);
          void send(Array.from(event.dataTransfer.files));
        }}
        className={cn(
          "rounded-xl border border-dashed transition-colors duration-150",
          over ? "border-brand bg-brand/5" : "border-border bg-card",
        )}
      >
        <button
          type="button"
          disabled={busy}
          onClick={() => inputRef.current?.click()}
          className={cn(
            "flex w-full flex-col items-start gap-3 rounded-xl px-6 py-8 text-left",
            "focus-visible:focus-ring disabled:opacity-50",
            busy ? "cursor-progress" : "hover:bg-surface-hover",
          )}
        >
          <FileUpIcon
            aria-hidden
            className="size-4 text-muted-foreground"
            strokeWidth={2}
          />
          <span className="space-y-1.5">
            <span className="block text-sm font-medium">
              {busy ? "Reading your files…" : "Drop your exports here, or browse"}
            </span>
            <span className="block max-w-prose text-xs leading-relaxed text-muted-foreground">
              A Razorpay settlement report, a bank statement (HDFC, ICICI or
              MT940), a Shopify order export, or a Delhivery COD remittance.
              The file is read by its header, never by its name — a renamed
              export is read as what it is.
            </span>
          </span>
        </button>
        {/* OUT OF THE TAB ORDER, DELIBERATELY. The button above is the whole
            affordance: it is labelled, it has a focus ring, and it opens the
            picker. Leaving this input focusable put a SECOND stop in the tab
            order that renders nothing at all -- a keyboard user tabbed past
            the dropzone onto an invisible control that opens an OS dialog,
            with no way to see where they were. */}
        <input
          ref={inputRef}
          type="file"
          multiple
          tabIndex={-1}
          aria-hidden
          className="sr-only"
          onChange={(event) => {
            void send(Array.from(event.target.files ?? []));
            // Cleared so re-picking the same file fires `change` again — which
            // is the whole re-upload demonstration.
            event.target.value = "";
          }}
        />
      </div>

      {outcomes.length > 0 ? (
        <ul className="space-y-2" aria-live="polite">
          {outcomes.map((outcome, i) => (
            <li key={`${outcome.name}-${i}`}>
              <OutcomeRow outcome={outcome} />
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

function OutcomeRow({ outcome }: { outcome: Outcome }) {
  if (outcome.kind === "pending") {
    return (
      <Shell tone="muted" name={outcome.name}>
        <span className="text-muted-foreground">
          Reading…
          <span className="sr-only"> uploading {outcome.name}</span>
        </span>
      </Shell>
    );
  }

  if (outcome.kind === "read" || outcome.kind === "held") {
    const { receipt } = outcome;
    const held = outcome.kind === "held";
    return (
      <Shell
        tone={held ? "muted" : "matched"}
        name={outcome.name}
        icon={
          held ? (
            <RotateCcwIcon aria-hidden className="size-3.5" strokeWidth={2} />
          ) : (
            <CheckIcon aria-hidden className="size-3.5" strokeWidth={2} />
          )
        }
      >
        <span className="space-y-1">
          <span className="block">
            {held ? (
              <>
                Already held — these exact bytes were uploaded before, so this
                is the upload you already have and nothing was written twice.
              </>
            ) : (
              <>
                Read as{" "}
                <span className="font-medium text-foreground">
                  {formatLabel(receipt.format_id)}
                </span>{" "}
                at{" "}
                <span className="tnum">
                  {(receipt.confidence * 100).toFixed(0)}%
                </span>{" "}
                confidence.
              </>
            )}
          </span>
          <span className="tnum block text-2xs text-muted-foreground">
            <span className="font-mono">{receipt.upload_id}</span> ·{" "}
            {receipt.record_count.toLocaleString("en-IN")}{" "}
            {receipt.record_count === 1 ? "record" : "records"} ·{" "}
            {receipt.quarantine_count.toLocaleString("en-IN")} quarantined ·{" "}
            {formatBytes(receipt.byte_size)}
          </span>
        </span>
      </Shell>
    );
  }

  if (outcome.kind === "refused") {
    return <Refusal name={outcome.name} refusal={outcome.refusal} />;
  }

  return (
    <Shell
      tone="error"
      name={outcome.name}
      icon={<XIcon aria-hidden className="size-3.5" strokeWidth={2} />}
    >
      <span className="text-error-fg">{outcome.message}</span>
    </Shell>
  );
}

/**
 * A refused file, with the arithmetic that refused it.
 *
 * The candidate table is the whole point. "We could not read this" is a dead
 * end; "the Razorpay layout scored 0.42 and the bar is 0.60" tells a merchant
 * their export is missing a column, and which direction to look in.
 */
function Refusal({
  name,
  refusal,
}: {
  name: string;
  refusal: UploadRefusedError;
}) {
  return (
    <div className="rounded-lg border border-excepted/50 bg-card px-4 py-3">
      <p className="flex flex-wrap items-baseline gap-x-2 text-xs">
        <span className="font-mono break-all">{name}</span>
        <span className="font-medium">
          {refusal.reason === "UNDECODABLE_FILE"
            ? "not a text file"
            : "no format recognised"}
        </span>
      </p>
      <p className="mt-2 max-w-prose text-xs leading-relaxed text-muted-foreground">
        {refusal.reason === "UNDECODABLE_FILE" ? (
          <>
            Nothing decoded these bytes. Detection reads the file itself, so a
            spreadsheet or an archive saved with a{" "}
            <code className="font-mono">.csv</code> extension fails here rather
            than parsing into nonsense. Export it as CSV from the portal that
            produced it.
          </>
        ) : (
          <>
            Detection is by header shape and every format scored below the{" "}
            <span className="tnum">{refusal.threshold.toFixed(2)}</span> bar, so
            nothing was guessed at. Check the first line of the file: an export
            with a column deselected in the portal is the usual cause.
          </>
        )}
      </p>

      {refusal.candidates.length > 0 ? (
        <table className="mt-3 w-full border-collapse text-left">
          <caption className="sr-only">
            Every format this build reads and the confidence it gave {name}, out
            of a required {refusal.threshold.toFixed(2)}.
          </caption>
          <tbody>
            {refusal.candidates.map((candidate) => (
              <tr key={candidate.format_id}>
                <th
                  scope="row"
                  className="py-1 pr-4 text-left text-2xs font-normal text-muted-foreground"
                >
                  {formatLabel(candidate.format_id)}
                </th>
                <td className="tnum py-1 text-right text-2xs text-muted-foreground">
                  {candidate.confidence.toFixed(2)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : null}
    </div>
  );
}

function Shell({
  tone,
  name,
  icon,
  children,
}: {
  tone: "matched" | "muted" | "error";
  name: string;
  icon?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <div
      className={cn(
        "flex items-start gap-3 rounded-lg border bg-card px-4 py-3 text-xs leading-relaxed",
        tone === "matched" && "border-matched/50",
        tone === "muted" && "border-border",
        tone === "error" && "border-destructive/30",
      )}
    >
      <span
        className={cn(
          "mt-0.5 shrink-0",
          tone === "matched" && "text-matched",
          tone === "muted" && "text-muted-foreground",
          tone === "error" && "text-error-fg",
        )}
      >
        {icon}
      </span>
      <div className="min-w-0 flex-1 space-y-1">
        <p className="font-mono text-2xs break-all text-muted-foreground">
          {name}
        </p>
        <div className="min-w-0">{children}</div>
      </div>
    </div>
  );
}

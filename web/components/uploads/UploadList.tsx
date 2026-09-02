"use client";

import * as React from "react";
import { formatTimestamp, fullTimestamp } from "@/lib/datetime";
import {
  UPLOAD_STATE,
  formatBytes,
  formatLabel,
  rolesOf,
  unitFor,
  type Upload,
} from "@/lib/uploads";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Th } from "@/components/explorer/paging";

const int = (n: number) => n.toLocaleString("en-IN");

/**
 * Every file this org has uploaded, with what reading it produced.
 *
 * **The selection lives here and the run is started from it.** A separate
 * "start a run" screen would have to re-list the same files, and a merchant
 * would then be choosing from a second copy of a list they were just looking
 * at.
 *
 * NOT PAGINATED, and that is a decision rather than an omission:
 * `GET /api/uploads` returns the org's whole set because an org has tens of
 * files, not thousands — a merchant uploads a handful per month. The
 * QUARANTINE listing is paginated, because one damaged export can carry four
 * hundred bad rows. If uploads ever page, the contract grows the parameters
 * first; a client-side slice over a full response would be paging that lies.
 *
 * Each row's counts are given BY ROLE rather than as one total. "412 records"
 * does not tell a merchant whether they have a bank statement; "412 bank
 * lines" does, and the run they are about to start needs to know.
 */
export function UploadList({
  uploads,
  selected,
  openId,
  onToggle,
  onSelectAll,
  onOpen,
}: {
  uploads: Upload[];
  selected: ReadonlySet<string>;
  openId: string | null;
  onToggle: (uploadId: string) => void;
  onSelectAll: (next: boolean) => void;
  onOpen: (uploadId: string) => void;
}) {
  // Only a file that produced records can feed a run, so only those are
  // selectable. Selecting one that produced nothing would build a request the
  // API correctly refuses -- and the refusal would arrive after the click.
  const usable = uploads.filter((upload) => upload.record_count > 0);
  const allSelected =
    usable.length > 0 && usable.every((upload) => selected.has(upload.upload_id));

  return (
    <div className="relative overflow-x-auto rounded-xl border border-border">
      <table className="w-full min-w-[62rem] border-collapse text-left">
        <caption className="sr-only">
          Every uploaded file, newest first: the format detected from its bytes,
          the confidence of that detection, the canonical records it produced by
          source, and how many rows were quarantined. Select the files a run
          should read.
        </caption>
        <thead>
          <tr className="border-b border-border bg-surface">
            <Th className="w-10 pl-4">
              <label className="flex items-center gap-2">
                <input
                  type="checkbox"
                  checked={allSelected}
                  disabled={usable.length === 0}
                  onChange={(event) => onSelectAll(event.target.checked)}
                  className="size-3.5 accent-[var(--brand)] focus-visible:focus-ring"
                />
                <span className="sr-only">
                  Select every file that produced records
                </span>
              </label>
            </Th>
            <Th className="w-64">File</Th>
            <Th className="w-56">Read as</Th>
            <Th className="w-52">Records</Th>
            <Th className="w-28 text-right">Quarantined</Th>
            <Th className="w-40">Uploaded</Th>
            <Th className="w-28 pr-4 text-right">Review</Th>
          </tr>
        </thead>
        <tbody>
          {uploads.map((upload) => (
            <Row
              key={upload.upload_id}
              upload={upload}
              selected={selected.has(upload.upload_id)}
              open={upload.upload_id === openId}
              onToggle={onToggle}
              onOpen={onOpen}
            />
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Row({
  upload,
  selected,
  open,
  onToggle,
  onOpen,
}: {
  upload: Upload;
  selected: boolean;
  open: boolean;
  onToggle: (uploadId: string) => void;
  onOpen: (uploadId: string) => void;
}) {
  const state = UPLOAD_STATE[upload.state];
  const usable = upload.record_count > 0;
  const roles = rolesOf(upload);
  const counts: Record<string, number> = {
    order: upload.order_count,
    psp_txn: upload.psp_txn_count,
    bank_line: upload.bank_line_count,
  };

  return (
    <tr
      className={cn(
        "border-b border-border transition-colors duration-150 last:border-b-0",
        open ? "bg-surface-selected" : "hover:bg-surface-hover",
      )}
    >
      <td className="py-3 pr-4 pl-4 align-top">
        <input
          type="checkbox"
          checked={selected}
          disabled={!usable}
          onChange={() => onToggle(upload.upload_id)}
          aria-label={`Include ${upload.filename} in the run`}
          className="mt-0.5 size-3.5 accent-[var(--brand)] focus-visible:focus-ring disabled:opacity-50"
        />
      </td>

      <th scope="row" className="py-3 pr-4 text-left font-normal align-top">
        <span className="block max-w-[16rem] truncate text-xs" title={upload.filename}>
          {upload.filename}
        </span>
        <span className="tnum mt-0.5 block font-mono text-2xs text-muted-foreground">
          {formatBytes(upload.byte_size)} ·{" "}
          <span title={`SHA-256 ${upload.content_sha256}`}>
            {upload.content_sha256.slice(0, 12)}
          </span>
        </span>
      </th>

      <td className="py-3 pr-4 align-top text-xs">
        <span className="block">{formatLabel(upload.format_id)}</span>
        <span className="tnum mt-0.5 block text-2xs text-muted-foreground">
          {(upload.confidence * 100).toFixed(0)}% confidence ·{" "}
          {upload.encoding || "no encoding"}
        </span>
      </td>

      <td className="py-3 pr-4 align-top text-xs">
        {roles.length > 0 ? (
          <span className="space-y-0.5">
            {roles.map((role) => (
              <span key={role} className="tnum block">
                {int(counts[role])} {unitFor(role, counts[role])}
              </span>
            ))}
          </span>
        ) : (
          <span
            className={cn(
              "text-2xs leading-relaxed",
              upload.state === "quarantined"
                ? "text-excepted-fg"
                : "text-muted-foreground",
            )}
          >
            {state.label}
          </span>
        )}
      </td>

      <td className="tnum py-3 pr-4 text-right align-top text-xs">
        {upload.quarantine_count === 0 ? (
          <>
            <span aria-hidden className="text-muted-foreground">
              &mdash;
            </span>
            <span className="sr-only">no rows were quarantined</span>
          </>
        ) : (
          <span className="font-medium text-excepted-fg">
            {int(upload.quarantine_count)}
          </span>
        )}
      </td>

      <td
        className="py-3 pr-4 align-top text-xs text-muted-foreground"
        title={fullTimestamp(upload.uploaded_at)}
      >
        {formatTimestamp(upload.uploaded_at)}
      </td>

      <td className="py-3 pr-4 text-right align-top">
        <Button
          variant="outline"
          size="sm"
          data-upload-trigger={upload.upload_id}
          onClick={() => onOpen(upload.upload_id)}
        >
          {open ? "Open" : "Review"}
          <span className="sr-only"> {upload.filename}</span>
        </Button>
      </td>
    </tr>
  );
}

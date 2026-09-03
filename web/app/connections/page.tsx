"use client";

import * as React from "react";
import {
  CheckIcon,
  RefreshCwIcon,
  TrashIcon,
  TriangleAlertIcon,
} from "lucide-react";

import { ApiError } from "@/lib/api";
import {
  connectionsApi,
  nextSyncHint,
  SYNC_STATUS,
  type Connection,
  type ConnectionSyncResult,
  type ConnectionTestResult,
} from "@/lib/connections";
import { formatTimestamp } from "@/lib/datetime";
import { useResource } from "@/lib/hooks";
import { EmptyState, ErrorState, LoadingBlock } from "@/components/States";
import { Panel, PanelHeader } from "@/components/Panel";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { cn } from "@/lib/utils";

/**
 * Mailboxes this console fetches statements from.
 *
 * There is no pull API for an Indian bank statement outside the RBI Account
 * Aggregator network, and registering as an FIU there is open only to
 * regulated entities. The bank already emails the statement every month, so
 * this reads that mail — and reads nothing else, which is most of what this
 * screen has to make legible.
 *
 * WHAT THIS SCREEN IS ACCOUNTABLE FOR is that a merchant hands over a
 * credential granting full mailbox read access. Three things are therefore
 * said out loud rather than buried: the password is stored encrypted and can
 * never be read back, the search is scoped to named senders and never the
 * whole mailbox, and whatever the filter declined is listed by name after a
 * sync. A privacy control nobody can see is a privacy control nobody trusts.
 */
export default function ConnectionsPage() {
  const [nonce, setNonce] = React.useState(0);
  const list = useResource<Connection[]>(`connections:${nonce}`, (signal) =>
    connectionsApi.list({ signal }),
  );
  const refresh = () => setNonce((n) => n + 1);

  return (
    <div className="mx-auto w-full max-w-[68rem] px-6 py-10 lg:px-8">
      <div className="space-y-2">
        <h1 className="text-xl font-bold tracking-tight">Mailbox</h1>
        <p className="max-w-[62ch] text-xs leading-relaxed text-muted-foreground">
          Statements arrive by email every month. Point this at that mailbox
          once and it fetches them on its own.
        </p>
      </div>

      <div className="mt-8 space-y-8">
        {/* `key` is the remount signal: when the saved connection changes
            identity the form rebuilds with fresh initial state instead of
            syncing props into state in an effect. */}
        <ConnectionForm
          key={list.data?.[0]?.id ?? "new"}
          existing={list.data?.[0] ?? null}
          onSaved={refresh}
        />

        {list.error ? (
          <ErrorState
            title="Connections did not load"
            error={list.error}
            recovery="The API may not be running. Saved mailboxes are unaffected."
          />
        ) : list.loading && list.data === null ? (
          <LoadingBlock label="Reading saved mailboxes" />
        ) : (list.data ?? []).length === 0 ? (
          <EmptyState
            title="No mailbox connected"
            reason="Once a mailbox is saved, statements are fetched every 30 days without anyone entering a password again."
          />
        ) : (
          <div className="space-y-6">
            {(list.data ?? []).map((c) => (
              <ConnectionCard key={c.id} connection={c} onChanged={refresh} />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

/* ---------------------------------------------------------------- the form */

/**
 * One mailbox, saved.
 *
 * The password field is ALWAYS blank on an existing connection, because the
 * API has no endpoint that returns it. A form that showed dots would be
 * inventing a value it does not have; this says what blank means instead, so
 * "leave it alone" and "I do not know it" stay distinguishable.
 */
function ConnectionForm({
  existing,
  onSaved,
}: {
  existing: Connection | null;
  onSaved: () => void;
}) {
  // Initialised FROM PROPS, and the caller remounts this form with a `key`
  // when the saved connection changes. Syncing props into state inside an
  // effect is the same thing written so that it renders once with the wrong
  // values first, and React now flags it.
  //
  // The two password fields are NOT seeded from `existing`: the API has no
  // endpoint that returns a stored secret, so there is nothing to seed them
  // with and a masked placeholder would be a value the form invented.
  const [host, setHost] = React.useState(existing?.imap_host ?? "imap.gmail.com");
  const [user, setUser] = React.useState(existing?.imap_user ?? "");
  const [password, setPassword] = React.useState("");
  const [pdfPassword, setPdfPassword] = React.useState("");
  const [senders, setSenders] = React.useState(existing?.senders ?? "");
  const [pattern, setPattern] = React.useState(existing?.filename_pattern ?? "");
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<unknown>(null);
  const [saved, setSaved] = React.useState(false);

  const editing = existing !== null;
  // On an edit the password may be left blank to keep the stored one. On a
  // first save it cannot be: there is nothing to keep.
  const canSubmit =
    host.trim() !== "" &&
    user.trim() !== "" &&
    senders.trim() !== "" &&
    (editing || password !== "");

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    setSaved(false);
    try {
      await connectionsApi.save({
        id: existing?.id ?? null,
        kind: "imap",
        imap_host: host.trim(),
        // Not form fields: there is one kind, 993 is the only sane IMAPS port,
        // and INBOX is where a bank's mail lands. Sent explicitly rather than
        // defaulted server-side so the stored row says what it is.
        imap_port: 993,
        folder: "INBOX",
        imap_user: user.trim(),
        // Gmail displays app passwords in groups of four. The spaces are
        // presentation, not value, and pasting them is the normal case.
        password: password.replace(/\s+/g, ""),
        pdf_password: pdfPassword.trim() === "" ? null : pdfPassword.trim(),
        senders: senders.trim(),
        filename_pattern: pattern.trim() === "" ? null : pattern.trim(),
      });
      setPassword("");
      setPdfPassword("");
      setSaved(true);
      onSaved();
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Panel>
      <PanelHeader title={editing ? "Edit mailbox" : "Connect a mailbox"} />
      <form onSubmit={submit} className="space-y-5 p-5">
        <div className="grid gap-5 sm:grid-cols-2">
          <Field
            id="imap-user"
            label="Email address"
            hint="The mailbox the bank sends statements to."
          >
            <Input
              id="imap-user"
              type="email"
              autoComplete="username"
              value={user}
              onChange={(e) => setUser(e.target.value)}
              placeholder="you@gmail.com"
            />
          </Field>

          <Field
            id="imap-password"
            label="App password"
            hint={
              editing
                ? "Stored and encrypted. Leave blank to keep the saved one."
                : "Gmail rejects your account password over IMAP — generate an App Password."
            }
          >
            <Input
              id="imap-password"
              type="password"
              autoComplete="new-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder={editing ? "•••••••••••••••• (unchanged)" : ""}
            />
          </Field>

          <Field
            id="imap-senders"
            label="Sender addresses"
            hint="Comma-separated. The search is scoped to these — never the whole mailbox."
          >
            <Input
              id="imap-senders"
              value={senders}
              onChange={(e) => setSenders(e.target.value)}
              placeholder="statements@yourbank.com"
            />
          </Field>

          <Field
            id="imap-pattern"
            label="Filename filter"
            hint="Optional. Only attachments matching this are read."
          >
            <Input
              id="imap-pattern"
              value={pattern}
              onChange={(e) => setPattern(e.target.value)}
              placeholder="statement"
            />
          </Field>

          <Field
            id="pdf-password"
            label="PDF password"
            hint="Optional. Most Indian bank statements arrive encrypted."
          >
            <Input
              id="pdf-password"
              type="password"
              autoComplete="new-password"
              value={pdfPassword}
              onChange={(e) => setPdfPassword(e.target.value)}
              placeholder={
                existing?.has_pdf_password ? "•••••••• (unchanged)" : ""
              }
            />
          </Field>

          <Field id="imap-host" label="IMAP host" hint="Rarely needs changing.">
            <Input
              id="imap-host"
              value={host}
              onChange={(e) => setHost(e.target.value)}
            />
          </Field>
        </div>

        <PrivacyNote />

        {error ? <SaveError error={error} /> : null}
        {saved && !error ? (
          <p className="flex items-center gap-2 text-xs text-matched-fg">
            <CheckIcon aria-hidden className="size-3.5" strokeWidth={2.5} />
            Saved. The password is encrypted at rest and cannot be read back.
          </p>
        ) : null}

        <Button type="submit" disabled={!canSubmit || busy}>
          {busy ? "Saving…" : editing ? "Update mailbox" : "Save mailbox"}
        </Button>
      </form>
    </Panel>
  );
}

/**
 * What this screen owes a merchant who is about to hand over a credential.
 *
 * Stated as three facts the code actually holds to, not as reassurance: the
 * store refuses to run keyless, the search is bounded, and credit reports are
 * excluded by name. Each one is checkable in the repository.
 */
function PrivacyNote() {
  return (
    <ul className="max-w-[68ch] space-y-1.5 border-l-2 border-border/60 pl-3 text-2xs leading-relaxed text-muted-foreground">
      <li>
        The password is encrypted with AES-256-GCM before it touches disk, bound
        to this row so a copied ciphertext cannot be replayed into another. No
        endpoint returns it.
      </li>
      <li>
        The mailbox is opened read-only. Nothing is marked read, moved or
        deleted, and the search is scoped to the senders above and the date
        window — never the whole mailbox.
      </li>
      <li>
        Credit reports are refused by name before they are read. A CIBIL or
        Experian attachment from the same sender is skipped, not quarantined.
      </li>
    </ul>
  );
}

/**
 * A save that failed.
 *
 * The 422 for a keyless build is a CONFIGURATION problem with a named fix, and
 * rendering it identically to a network failure would send a merchant looking
 * for a bug that is not there.
 */
function SaveError({ error }: { error: unknown }) {
  const detail =
    error instanceof ApiError && typeof error.body === "object" && error.body
      ? String((error.body as { detail?: unknown }).detail ?? error.message)
      : error instanceof Error
        ? error.message
        : String(error);
  const keyless = detail.includes("RECON_BLOB_KEY");

  return (
    <div className="brut-flat border-error-fg/40 bg-error-surface p-3.5">
      <p className="flex items-start gap-2 text-xs leading-relaxed text-error-fg">
        <TriangleAlertIcon
          aria-hidden
          className="mt-0.5 size-3.5 shrink-0"
          strokeWidth={2.25}
        />
        <span>
          <span className="font-semibold">
            {keyless ? "Encryption is not configured" : "Could not save"}
          </span>
          <br />
          {detail}
          {keyless ? (
            <>
              {" "}
              Nothing was stored. The API refuses to hold a credential it cannot
              encrypt rather than writing one in plaintext.
            </>
          ) : null}
        </span>
      </p>
    </div>
  );
}

/* -------------------------------------------------------------- the card */

function ConnectionCard({
  connection,
  onChanged,
}: {
  connection: Connection;
  onChanged: () => void;
}) {
  const [busy, setBusy] = React.useState<"test" | "sync" | "delete" | null>(
    null,
  );
  const [test, setTest] = React.useState<ConnectionTestResult | null>(null);
  const [sync, setSync] = React.useState<ConnectionSyncResult | null>(null);
  const [error, setError] = React.useState<string | null>(null);

  const status = SYNC_STATUS[connection.last_sync_status] ?? SYNC_STATUS.never;

  async function run(
    kind: "test" | "sync" | "delete",
    fn: () => Promise<unknown>,
  ) {
    setBusy(kind);
    setError(null);
    setTest(null);
    setSync(null);
    try {
      const result = await fn();
      if (kind === "test") setTest(result as ConnectionTestResult);
      if (kind === "sync") setSync(result as ConnectionSyncResult);
      onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(null);
    }
  }

  return (
    <Panel>
      <PanelHeader title={connection.imap_user} />
      <div className="space-y-4 p-5">
        <dl className="flex flex-wrap items-baseline gap-x-8 gap-y-2 text-xs">
          <Meta label="Senders" value={connection.senders} />
          <Meta
            label="Filename filter"
            value={connection.filename_pattern ?? "none"}
          />
          <Meta
            label="Password"
            value={connection.has_password ? "Stored, encrypted" : "Not set"}
          />
          <Meta
            label="Last sync"
            value={
              connection.last_sync_at
                ? formatTimestamp(connection.last_sync_at)
                : "—"
            }
          />
        </dl>

        <p
          className={cn(
            "flex items-center gap-2 text-xs font-semibold",
            status.tone === "good" && "text-matched-fg",
            status.tone === "bad" && "text-error-fg",
            status.tone === "neutral" && "text-muted-foreground",
          )}
        >
          {status.label}
          <span className="font-normal text-muted-foreground">
            {nextSyncHint(connection)}
          </span>
        </p>

        {connection.last_sync_error ? (
          <p className="max-w-[68ch] font-mono text-2xs leading-relaxed text-error-fg">
            {connection.last_sync_error}
          </p>
        ) : null}

        <div className="flex flex-wrap gap-2.5">
          <Button
            variant="outline"
            disabled={busy !== null}
            onClick={() => run("test", () => connectionsApi.test(connection.id))}
          >
            {busy === "test" ? "Testing…" : "Test connection"}
          </Button>
          <Button
            disabled={busy !== null}
            onClick={() => run("sync", () => connectionsApi.sync(connection.id))}
          >
            <RefreshCwIcon aria-hidden strokeWidth={2.25} />
            {busy === "sync" ? "Fetching…" : "Fetch now"}
          </Button>
          <Button
            variant="destructive"
            disabled={busy !== null}
            onClick={() =>
              run("delete", () => connectionsApi.remove(connection.id))
            }
          >
            <TrashIcon aria-hidden strokeWidth={2.25} />
            {busy === "delete" ? "Removing…" : "Remove"}
          </Button>
        </div>

        {error ? <p className="text-xs text-error-fg">{error}</p> : null}
        {test ? <TestResult result={test} /> : null}
        {sync ? <SyncResult result={sync} /> : null}
      </div>
    </Panel>
  );
}

/**
 * "Your password is wrong" and "your filter matches nothing" both look like
 * zero attachments from a fetch. This answers only the first question, which
 * is what makes the two separable.
 */
function TestResult({ result }: { result: ConnectionTestResult }) {
  return (
    <p
      className={cn(
        "flex items-start gap-2 text-xs leading-relaxed",
        result.ok ? "text-matched-fg" : "text-error-fg",
      )}
    >
      {result.ok ? (
        <CheckIcon aria-hidden className="mt-0.5 size-3.5" strokeWidth={2.5} />
      ) : (
        <TriangleAlertIcon
          aria-hidden
          className="mt-0.5 size-3.5"
          strokeWidth={2.25}
        />
      )}
      <span>{result.detail}</span>
    </p>
  );
}

/**
 * A fetch, counted.
 *
 * `skipped_names` is rendered rather than summarised, because the attachments
 * the filter declined are the whole point of the filter — a credit report
 * silently not fetched is indistinguishable from a credit report the bank
 * never sent, and only one of those means the control is working.
 */
function SyncResult({ result }: { result: ConnectionSyncResult }) {
  const n = result.upload_ids.length;
  return (
    <div className="brut-flat bg-surface p-3.5">
      <p className="text-xs">
        <span className="font-semibold">
          {n === 0 ? "No new statements" : `${n} statement${n === 1 ? "" : "s"} ingested`}
        </span>{" "}
        <span className="text-muted-foreground">
          from {result.window_start} to {result.window_end}
          {result.quarantine_count > 0
            ? `, ${result.quarantine_count} row${result.quarantine_count === 1 ? "" : "s"} quarantined`
            : null}
          .
        </span>
      </p>
      {result.skipped_names.length > 0 ? (
        <p className="mt-1.5 text-2xs leading-relaxed text-muted-foreground">
          Skipped without reading:{" "}
          <span className="font-mono">{result.skipped_names.join(", ")}</span>
        </p>
      ) : null}
    </div>
  );
}

/* --------------------------------------------------------------- fragments */

function Field({
  id,
  label,
  hint,
  children,
}: {
  id: string;
  label: string;
  hint: string;
  children: React.ReactNode;
}) {
  return (
    <div className="space-y-1.5">
      <Label htmlFor={id} className="text-xs font-semibold">
        {label}
      </Label>
      {children}
      <p className="text-2xs leading-relaxed text-muted-foreground">{hint}</p>
    </div>
  );
}

function Meta({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-baseline gap-2">
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="font-medium">{value}</dd>
    </div>
  );
}

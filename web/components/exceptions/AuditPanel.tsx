"use client";

import { api } from "@/lib/api";
import { useResource } from "@/lib/hooks";
import { formatINR } from "@/lib/money";
import {
  AUDIT_ACTOR_LABEL,
  REASON_CODE_DESCRIPTION,
  REASON_CODE_LABEL,
  UNRESOLVABLE_BY_DESIGN,
  VERDICT_LABEL,
  VERIFIER_CHECKS,
  VERIFIER_CHECK_CONFLATION,
  VERIFIER_CHECK_DESCRIPTION,
  VERIFIER_CHECK_LABEL,
} from "@/lib/labels";
import { cn } from "@/lib/utils";
import type {
  AuditEntry,
  ReconExceptionAudited,
  ReconExceptionDetail,
} from "@/lib/types";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { LoadingBlock, Skeleton } from "@/components/States";
import { SubjectRecordView } from "@/components/SubjectRecordView";

/**
 * The audit slide-over: one exception, end to end.
 *
 * The subject record, the reason it was raised, the hypothesis if one was made,
 * the verifier's verdict, and the specific check that rejected it. The check is
 * read off `failed_check`, a typed field on the contract; `verifier_reason` is
 * free prose for a human and is rendered verbatim and never parsed. Those are
 * two different fields carrying two different kinds of information, and
 * recovering the first from the second is the bug this panel exists not to
 * have.
 *
 * OPENS FROM TWO PLACES, WHICH IS WHY IT TAKES AN ID AND NOT JUST A ROW. A
 * click in the table already has the whole `ReconExceptionDetail`, so the panel
 * paints immediately and only fetches the audit trail. A link straight to
 * `?exc=…` has nothing but an id, so everything comes from
 * `GET /api/exceptions/{id}` and the header renders a skeleton until it lands.
 * A reviewer can send someone a URL that opens one exception, which is most of
 * why the exception list is a route rather than a tab.
 *
 * A rejected hypothesis is shown in full, never filtered out, never collapsed
 * by default and never styled as an error. A rejection is the visible evidence
 * that the guardrail fires.
 */
export function AuditPanel({
  exceptionId,
  seed,
  onClose,
}: {
  exceptionId: string | null;
  /** The row the table already holds, when the panel was opened from one. */
  seed: ReconExceptionDetail | null;
  onClose: () => void;
}) {
  const { data, error, loading, refresh } = useResource<ReconExceptionAudited>(
    `exception:${exceptionId ?? "none"}`,
    (signal) => api.getException(exceptionId!, { signal }),
    exceptionId !== null,
  );

  const fetched = data?.exception_id === exceptionId ? data : null;
  const row: ReconExceptionDetail | null =
    seed?.exception_id === exceptionId ? seed : fetched;
  const trail: AuditEntry[] | null = fetched?.audit_trail ?? null;

  return (
    <Sheet
      open={exceptionId !== null}
      onOpenChange={(open) => !open && onClose()}
    >
      <SheetContent
        side="right"
        className="w-full gap-0 overflow-y-auto sm:max-w-2xl"
      >
        {exceptionId === null ? null : row === null ? (
          <PendingHeader exceptionId={exceptionId} error={error} onRetry={refresh} />
        ) : (
          <>
            <SheetHeader className="gap-2 border-b border-border px-6 py-5">
              <SheetTitle className="text-lg">
                {REASON_CODE_LABEL[row.reason_code]}
              </SheetTitle>
              <SheetDescription className="text-xs leading-relaxed">
                {REASON_CODE_DESCRIPTION[row.reason_code]}
              </SheetDescription>
              {/* Identity sits below the heading, never above it: an id set in
                  small type over a title is a kicker, and this one is data. */}
              <dl className="flex flex-wrap items-baseline gap-x-6 gap-y-1 pt-1">
                <Field label="Subject">
                  <span className="font-mono text-xs">{row.subject_id}</span>
                </Field>
                <Field label="Amount">
                  <span className="money text-xs font-medium">
                    {formatINR(row.amount)}
                  </span>
                </Field>
                <Field label="Exception">
                  <span className="font-mono text-xs break-all">
                    {row.exception_id}
                  </span>
                </Field>
              </dl>
            </SheetHeader>

            <div className="space-y-8 px-6 py-6">
              {UNRESOLVABLE_BY_DESIGN.has(row.reason_code) ? (
                <p className="rounded-lg border border-excepted/40 bg-excepted/10 px-4 py-3 text-xs leading-relaxed">
                  <span className="font-medium">
                    This subject is unresolvable by construction.
                  </span>{" "}
                  Two candidate settlements satisfy the arithmetic identically on
                  the same date, so either assignment would be a guess and
                  iteration order would be the tie-breaker. Leaving it here is
                  the correct outcome, and it is what{" "}
                  <code className="font-mono">trap_capture_rate</code> measures.
                </p>
              ) : null}

              <SubjectRecordView
                subjectType={row.subject_type}
                subject={row.subject}
              />

              <HypothesisSection row={row} />

              <section>
                <SectionHeading>Audit trail</SectionHeading>
                {error ? (
                  <p
                    className="mt-3 text-xs leading-relaxed text-error-fg"
                    role="alert"
                  >
                    The trail did not load: {error.message}. The exception and
                    its subject above came with the table row and are unaffected;
                    close and reopen this panel to try the trail again.
                  </p>
                ) : loading ? (
                  <LoadingBlock
                    label={`Loading the audit trail for ${row.subject_id}`}
                    lines={4}
                    className="mt-3"
                  />
                ) : trail && trail.length > 0 ? (
                  <ol className="mt-3 space-y-0">
                    {trail.map((entry) => (
                      <AuditRow key={entry.entry_id} entry={entry} />
                    ))}
                  </ol>
                ) : (
                  <p className="mt-3 max-w-[62ch] text-xs leading-relaxed text-muted-foreground">
                    No entries were recorded for this subject. The trail is
                    empty rather than missing — the request succeeded and
                    returned none.
                  </p>
                )}
              </section>
            </div>
          </>
        )}
      </SheetContent>
    </Sheet>
  );
}

/**
 * Opened straight from a URL, with an id and nothing else. The shape of what is
 * coming is known, so it is a skeleton rather than a spinner — and a failure
 * here is a different failure from the trail's, because nothing has arrived.
 */
function PendingHeader({
  exceptionId,
  error,
  onRetry,
}: {
  exceptionId: string;
  error: Error | null;
  onRetry: () => void;
}) {
  return (
    <>
      <SheetHeader className="gap-2 border-b border-border px-6 py-5">
        <SheetTitle className="text-lg">
          {error ? "This exception did not load" : "Loading exception"}
        </SheetTitle>
        <SheetDescription className="text-xs leading-relaxed">
          <span className="font-mono">{exceptionId}</span>
        </SheetDescription>
      </SheetHeader>
      <div className="px-6 py-6">
        {error ? (
          <div role="alert" className="space-y-3">
            <p className="max-w-[62ch] text-xs leading-relaxed">
              <code className="font-mono">GET /api/exceptions/{exceptionId}</code>{" "}
              failed. If this panel was opened from a link, the id in the URL may
              belong to a different run, or to a run that no longer exists — the
              exception list behind this panel will still load either way.
            </p>
            <p className="font-mono text-2xs break-words text-muted-foreground">
              {error.message}
            </p>
            <button
              type="button"
              onClick={onRetry}
              className="rounded-md border border-border px-2.5 py-1 text-xs transition-colors duration-150 hover:bg-surface-hover active:bg-surface-active focus-visible:focus-ring"
            >
              Try again
            </button>
          </div>
        ) : (
          <div className="space-y-4" aria-busy="true">
            <span className="sr-only" role="status">
              Loading exception {exceptionId}
            </span>
            <Skeleton className="h-3 w-40" />
            <Skeleton className="h-3 w-full" />
            <Skeleton className="h-3 w-4/5" />
            <Skeleton className="h-24 w-full" />
          </div>
        )}
      </div>
    </>
  );
}

/**
 * The hypothesis, the verdict, and which of the checks refused it.
 *
 * `failed_check` is read directly. The panel additionally names what the label
 * does NOT tell you: `existence` carries three separate rules, so seeing it
 * here does not identify which one fired. The free-text reason underneath is
 * where that shows — read by a human, never parsed by this component.
 */
function HypothesisSection({ row }: { row: ReconExceptionDetail }) {
  const rejected = row.verifier_verdict === "rejected";
  const conflation = row.failed_check
    ? VERIFIER_CHECK_CONFLATION[row.failed_check]
    : undefined;

  return (
    <section>
      <SectionHeading>Analyst hypothesis</SectionHeading>

      {row.llm_hypothesis === null ? (
        <p className="mt-3 max-w-[62ch] text-xs leading-relaxed text-muted-foreground">
          No hypothesis was attempted.{" "}
          <span className="font-mono">verifier_verdict</span> is{" "}
          <span className="font-mono">not_attempted</span> and{" "}
          <span className="font-mono">failed_check</span> is null — the
          deterministic tiers declined this subject and it was itemised without
          the model being asked. That is not the same as a model that was asked
          and had nothing to say.
        </p>
      ) : (
        <div className="mt-3 space-y-4">
          <blockquote className="rounded-lg border border-border bg-muted/40 px-4 py-3 text-xs leading-relaxed">
            {row.llm_hypothesis}
          </blockquote>

          <div className="flex flex-wrap items-center gap-2">
            <span
              className={cn(
                "inline-flex items-center rounded-full border px-2 py-0.5 text-2xs font-medium",
                rejected
                  ? "border-rejected/40 bg-rejected/10 text-rejected-fg"
                  : "border-border text-muted-foreground",
              )}
            >
              Verifier: {VERDICT_LABEL[row.verifier_verdict]}
            </span>
            {row.failed_check !== null ? (
              <span className="inline-flex items-center rounded-full border border-border px-2 py-0.5 font-mono text-2xs">
                failed_check = {row.failed_check}
              </span>
            ) : null}
          </div>

          {row.failed_check !== null ? (
            <div className="rounded-lg border border-border px-4 py-3">
              <p className="text-xs font-medium">
                {VERIFIER_CHECK_LABEL[row.failed_check]} — check{" "}
                <span className="tnum">
                  {VERIFIER_CHECKS.indexOf(row.failed_check) + 1}
                </span>{" "}
                of <span className="tnum">{VERIFIER_CHECKS.length}</span>
              </p>
              <p className="mt-1.5 max-w-[62ch] text-xs leading-relaxed text-muted-foreground">
                {VERIFIER_CHECK_DESCRIPTION[row.failed_check]}
              </p>
              <p className="mt-2 max-w-[62ch] text-2xs leading-relaxed text-muted-foreground">
                The verifier returns on the first failure, so this names the
                earliest rule the hypothesis broke, not every rule it broke.
              </p>
              {conflation ? (
                <p className="mt-2 max-w-[62ch] border-t border-border pt-2 text-2xs leading-relaxed text-muted-foreground">
                  {conflation}
                </p>
              ) : null}
            </div>
          ) : null}

          {row.verifier_reason !== null ? (
            <div>
              <p className="text-2xs font-medium text-muted-foreground">
                Verifier reason, verbatim
              </p>
              <p className="mt-1.5 max-w-[62ch] text-xs leading-relaxed">
                {row.verifier_reason}
              </p>
              <p className="mt-1.5 max-w-[62ch] text-2xs leading-relaxed text-muted-foreground">
                Prose, written for a human. The check above is not derived from
                it — it is its own typed field on the contract, and parsing this
                sentence to find one would be a guess dressed as a fact.
              </p>
            </div>
          ) : null}

          {rejected ? (
            <p className="max-w-[62ch] text-2xs leading-relaxed text-muted-foreground">
              The model proposed a resolution and the verifier refused it, so the
              subject stays an exception. That is the guardrail working. Nothing
              the model said became a number: every money field on an accepted
              match is recomputed from the legs by the matcher&apos;s own
              reconstruction, and the hypothesis&apos;s own confidence is
              recorded in evidence and read by no check.
            </p>
          ) : null}
        </div>
      )}
    </section>
  );
}

function AuditRow({ entry }: { entry: AuditEntry }) {
  return (
    <li className="grid grid-cols-[2.5rem_1fr] gap-x-3 border-l border-border py-2.5 pl-4">
      <span className="tnum text-2xs text-muted-foreground">
        {String(entry.sequence).padStart(2, "0")}
      </span>
      <div className="min-w-0">
        <div className="flex flex-wrap items-baseline gap-x-2">
          <span className="font-mono text-xs">{entry.rule}</span>
          <span className="text-2xs text-muted-foreground">
            {AUDIT_ACTOR_LABEL[entry.actor]} · {entry.stage} ·{" "}
            <span className="tnum">{entry.confidence.toFixed(2)}</span>
          </span>
        </div>
        <p className="mt-1 text-xs leading-relaxed text-muted-foreground">
          {entry.evidence}
        </p>
      </div>
    </li>
  );
}

function SectionHeading({ children }: { children: React.ReactNode }) {
  return <h3 className="text-sm font-medium tracking-tight">{children}</h3>;
}

function Field({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <div className="inline-flex items-baseline gap-2">
      <dt className="text-2xs text-muted-foreground">{label}</dt>
      <dd>{children}</dd>
    </div>
  );
}

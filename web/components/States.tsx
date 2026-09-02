import * as React from "react";
import { AlertTriangleIcon, InboxIcon } from "lucide-react";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";

/**
 * The three states every data surface in this console can be in, written once.
 *
 * They exist as one module because the failure they prevent is inconsistency:
 * a table that says "Loading…" next to a panel that shows a spinner next to a
 * chart that shows nothing at all teaches a reader that blank means three
 * different things.
 */

/** A block standing in for content whose shape is already known. */
export function Skeleton({
  className,
  ...props
}: React.ComponentProps<"div">) {
  return (
    <div
      aria-hidden
      className={cn("skeleton h-4 w-full", className)}
      {...props}
    />
  );
}

/**
 * A loading state that reports what it is waiting for.
 *
 * `label` is announced to assistive technology; the bars are decoration for
 * everyone else. A screen reader gets "Loading the exception list", not a
 * silent region that changes under it.
 */
export function LoadingBlock({
  label,
  lines = 3,
  className,
}: {
  label: string;
  lines?: number;
  className?: string;
}) {
  return (
    <div className={cn("space-y-3", className)} aria-busy="true">
      <span className="sr-only" role="status">
        {label}
      </span>
      {Array.from({ length: lines }, (_, i) => (
        <Skeleton
          key={i}
          className="h-4"
          style={{ width: `${[100, 82, 64, 91, 73][i % 5]}%` }}
        />
      ))}
    </div>
  );
}

/**
 * Nothing here, and why.
 *
 * `reason` is required. An empty state that does not say why it is empty is
 * indistinguishable from a broken one, and in a reconciliation console the
 * difference between "this run produced no exceptions" and "the exceptions
 * did not load" is the difference between a result and a bug.
 */
export function EmptyState({
  title,
  reason,
  action,
  className,
}: {
  title: string;
  reason: React.ReactNode;
  action?: React.ReactNode;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "flex flex-col items-start gap-3 rounded-xl border border-dashed border-border px-6 py-10",
        className,
      )}
    >
      <InboxIcon
        aria-hidden
        className="size-4 shrink-0 text-muted-foreground"
        strokeWidth={2}
      />
      <div className="space-y-1.5">
        <p className="text-sm font-medium">{title}</p>
        <p className="max-w-prose text-xs leading-relaxed text-muted-foreground">
          {reason}
        </p>
      </div>
      {action}
    </div>
  );
}

/**
 * A failure that names the problem and the recovery.
 *
 * `onRetry` is optional because not every failure is retryable, but when a
 * surface can retry it must offer it here rather than expecting a page
 * reload.
 */
export function ErrorState({
  title,
  error,
  recovery,
  onRetry,
  className,
}: {
  title: string;
  error: Error;
  recovery: React.ReactNode;
  onRetry?: () => void;
  className?: string;
}) {
  return (
    <div
      role="alert"
      className={cn(
        "rounded-xl border border-destructive/30 bg-error-surface px-6 py-5",
        className,
      )}
    >
      <div className="flex items-start gap-3">
        <AlertTriangleIcon
          aria-hidden
          className="mt-0.5 size-4 shrink-0 text-error-fg"
          strokeWidth={2}
        />
        <div className="min-w-0 space-y-2">
          <p className="text-sm font-medium text-error-fg">{title}</p>
          <p className="max-w-prose text-xs leading-relaxed text-foreground">
            {recovery}
          </p>
          <p className="font-mono text-2xs break-words text-muted-foreground">
            {error.message}
          </p>
          {onRetry ? (
            <Button variant="outline" size="sm" onClick={onRetry} className="mt-1">
              Try again
            </Button>
          ) : null}
        </div>
      </div>
    </div>
  );
}

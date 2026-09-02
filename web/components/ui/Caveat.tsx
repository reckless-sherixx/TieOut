import * as React from "react";

import { cn } from "@/lib/utils";

/**
 * One qualifying passage, folded.
 *
 * This console's honesty commitments cost nothing to keep and a great deal to
 * read all at once. Measured 2026-09-02: the run summary rendered ~600 words
 * and the tier page ~500 before either showed a figure, because every table
 * carried three sentences of justification above it. The argument arrived
 * before the number it was defending, and the effect was the opposite of the
 * intent — a reviewer who cannot find the number cannot check it.
 *
 * Nothing is deleted here. Every sentence stays in the DOM: findable with
 * ctrl-F, present in print, reachable by a screen reader on demand. It simply
 * stops competing with the figure it qualifies.
 *
 * Native `<details>` rather than a custom disclosure, because it is keyboard
 * operable for free, exposes `group` to assistive technology, and still opens
 * if JavaScript fails.
 */
export function Caveat({
  summary,
  defaultOpen = false,
  className,
  children,
}: {
  /** The one-line prompt. Says what the reader gains by opening it. */
  summary: string;
  /**
   * Open on arrival. Reserved for a caveat that changes how the number is
   * READ — not merely one that adds detail. If everything is open by default,
   * this component has achieved nothing.
   */
  defaultOpen?: boolean;
  className?: string;
  children: React.ReactNode;
}) {
  return (
    <details
      open={defaultOpen}
      className={cn(
        "group mt-3 max-w-[68ch] border-l-2 border-border/60 pl-3",
        className,
      )}
    >
      <summary className="cursor-pointer list-none text-2xs text-muted-foreground underline decoration-dotted underline-offset-2 focus-visible:focus-ring [&::-webkit-details-marker]:hidden">
        {summary}
      </summary>
      <div className="mt-1.5 space-y-2 text-2xs leading-relaxed text-muted-foreground">
        {children}
      </div>
    </details>
  );
}

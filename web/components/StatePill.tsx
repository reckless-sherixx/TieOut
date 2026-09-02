import { cn } from "@/lib/utils";
import { RUN_STATE_LABEL } from "@/lib/labels";
import type { RunState } from "@/lib/types";

/**
 * A run's lifecycle state. Deliberately quiet: `failed` is the only state that
 * gets a colour, because it is the only one that is actually wrong.
 */
export function StatePill({
  state,
  className,
}: {
  state: RunState;
  className?: string;
}) {
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 border-2 border-[var(--ink)] px-2 py-0.5 text-2xs font-semibold",
        state === "failed" && "border-destructive/30 text-destructive",
        state === "running" && "border-brand/40 text-brand",
        className,
      )}
    >
      <span
        aria-hidden
        className={cn(
          "size-1.5 rounded-none",
          state === "completed" && "bg-foreground/50",
          state === "running" && "animate-pulse bg-brand",
          state === "pending" && "bg-muted-foreground/50",
          state === "failed" && "bg-destructive",
        )}
      />
      {RUN_STATE_LABEL[state]}
    </span>
  );
}

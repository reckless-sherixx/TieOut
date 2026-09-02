import * as React from "react";
import { cn } from "@/lib/utils";

/**
 * The one surface primitive. A bordered card on the background, generous
 * padding, no shadow — depth is carried by the border and the whitespace, so
 * the same component reads correctly in light and in dark.
 */
export function Panel({
  className,
  ...props
}: React.ComponentProps<"section">) {
  return (
    <section
      className={cn(
        "rounded-xl border border-border bg-card text-card-foreground",
        className,
      )}
      {...props}
    />
  );
}

export function PanelHeader({
  title,
  description,
  action,
  className,
}: {
  title: React.ReactNode;
  description?: React.ReactNode;
  action?: React.ReactNode;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "flex flex-wrap items-start justify-between gap-4 px-6 pt-5 pb-4",
        className,
      )}
    >
      <div className="space-y-1">
        <h2 className="text-sm font-medium tracking-tight">{title}</h2>
        {description ? (
          <p className="max-w-2xl text-xs leading-relaxed text-muted-foreground">
            {description}
          </p>
        ) : null}
      </div>
      {action ? <div className="shrink-0">{action}</div> : null}
    </div>
  );
}

/* `SectionTitle` used to live here: a small uppercase letterspaced heading
   that was, in practice, always set directly above the real heading of the
   page. That is an eyebrow, and an eyebrow is a heading that did not trust
   itself. The headings carry their own weight now, so it is gone rather than
   deprecated — a deprecated export is an invitation. */

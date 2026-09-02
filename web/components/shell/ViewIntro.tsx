/**
 * The heading of one view inside a run.
 *
 * A heading and a lede, and nothing above the heading. The run's identity is
 * already on the layout header directly above this, so repeating it — or
 * setting a small label over the heading to say which section you are in —
 * would be saying twice what the navigation already says once.
 */
export function ViewIntro({
  title,
  lede,
  action,
}: {
  title: string;
  lede: string;
  action?: React.ReactNode;
}) {
  return (
    <div className="flex flex-wrap items-start justify-between gap-x-8 gap-y-4">
      <div className="space-y-2">
        {/* h2, not h1: the run id in RunScope is the page heading. A view is a
            section within that run, so promoting it here would give every run
            route two h1s and break the document outline for a screen reader. */}
        <h2 className="text-lg font-medium tracking-tight">{title}</h2>
        <p className="max-w-[72ch] text-xs leading-relaxed text-muted-foreground">
          {lede}
        </p>
      </div>
      {action ? <div className="shrink-0">{action}</div> : null}
    </div>
  );
}

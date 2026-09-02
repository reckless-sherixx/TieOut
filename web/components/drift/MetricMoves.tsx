"use client";

import * as React from "react";
import { ChevronRightIcon } from "lucide-react";
import {
  MAGNITUDE_MATERIAL_RATIO,
  MATERIALITY_RULE_NOTE,
  RATE_MATERIAL_DELTA,
  formatMetricDelta,
  formatMetricValue,
  profileOf,
  relativeMove,
  splitMoves,
} from "@/lib/drift";
import { formatRate } from "@/lib/money";
import { cn } from "@/lib/utils";
import type { MetricMove } from "@/lib/types";

/**
 * Every metric of the contract, before and after, with the ones that cleared a
 * threshold set apart from the ones that did not.
 *
 * `material` IS READ, NEVER RECOMPUTED. Detection is deterministic and lives in
 * `core/drift/compare.py` against named constants; this table renders the flag
 * the API sent and explains the rule behind it. A UI that applied its own
 * threshold would be a second detector that can disagree with the first.
 *
 * EVERY METRIC IS LISTED, MOVED OR NOT. The API reports one entry per numeric
 * field whether or not it changed, and the `material` flag is what separates a
 * finding from a row — so a metric that held steady is a result here, in
 * exactly the way a tier that matched nothing is a result on the summary. The
 * ordering inside each group is the API's, which is the declaration order of
 * `Metrics`: accuracy rates, then throughput and cost, then the rupee figures.
 */
export function MetricMoves({ moves }: { moves: readonly MetricMove[] }) {
  const [open, setOpen] = React.useState<string | null>(null);
  const { material, unchanged } = splitMoves(moves);

  return (
    <div className="space-y-5">
      <div className="relative overflow-x-auto rounded-xl border border-border">
        <table className="w-full min-w-[46rem] border-collapse text-left">
          <caption className="sr-only">
            Every compared metric, before and after, grouped by whether the move
            cleared this metric&apos;s threshold
          </caption>
          <thead>
            <tr className="border-b border-border bg-surface">
              <Th className="w-9 pl-4">
                <span className="sr-only">Expand</span>
              </Th>
              <Th className="w-44">Metric</Th>
              <Th className="w-32 text-right">Baseline</Th>
              <Th className="w-32 text-right">This run</Th>
              <Th className="w-32 pr-4 text-right">Change</Th>
            </tr>
          </thead>

          <MoveGroup
            label={
              material.length === 0
                ? "Material — nothing cleared a threshold on this comparison"
                : "Material — cleared this metric’s own threshold"
            }
            moves={material}
            emptyNote="No metric moved far enough to be a finding. That is a result about these two runs, not an empty section."
            material
            open={open}
            onToggle={(metric) =>
              setOpen((cur) => (cur === metric ? null : metric))
            }
          />

          {/* NOT "below threshold": throughput lives in this group and has no
              threshold to be below. It is reported and never flagged, because
              wall clock on shared hardware is the one figure in this project
              that does not reproduce on another machine. */}
          <MoveGroup
            label="Not material — unchanged, under threshold, or never flagged"
            moves={unchanged}
            emptyNote="Every compared metric cleared its threshold."
            open={open}
            onToggle={(metric) =>
              setOpen((cur) => (cur === metric ? null : metric))
            }
          />
        </table>
      </div>

      <p className="max-w-[72ch] text-xs leading-relaxed text-muted-foreground">
        Materiality is the API&apos;s, computed from the two numbers and a named
        constant and from nothing else —{" "}
        {/* The rate rule is an ABSOLUTE delta on a 0.0-1.0 figure. Writing it
            as "1%" beside metrics that are themselves percentages reads as a
            relative change, which is the other rule on this same list. */}
        <span className="tnum">{RATE_MATERIAL_DELTA}</span> absolute for the
        bounded rates, any change at all for the three metrics whose correct
        value is a known constant,{" "}
        <span className="tnum">
          {formatRate(MAGNITUDE_MATERIAL_RATIO, 0)}
        </span>{" "}
        relative for the unbounded magnitudes, and never for throughput. Open
        any row for the rule that applies to it. Nothing on this page applies a
        threshold of its own.
      </p>
    </div>
  );
}

function MoveGroup({
  label,
  moves,
  emptyNote,
  material,
  open,
  onToggle,
}: {
  label: string;
  moves: MetricMove[];
  emptyNote: string;
  material?: boolean;
  open: string | null;
  onToggle: (metric: string) => void;
}) {
  return (
    <tbody>
      <tr className="border-b border-border bg-surface/60">
        <td colSpan={5} className="px-4 py-2 text-2xs font-medium">
          <span className="inline-flex items-center gap-2">
            <span
              aria-hidden
              className={cn(
                "size-1.5 shrink-0 rounded-[2px]",
                material ? "bg-excepted" : "bg-muted-foreground/50",
              )}
            />
            {label}
          </span>
        </td>
      </tr>

      {moves.length === 0 ? (
        <tr className="border-b border-border">
          <td />
          <td
            colSpan={4}
            className="py-2.5 pr-4 text-xs leading-relaxed text-muted-foreground"
          >
            {emptyNote}
          </td>
        </tr>
      ) : (
        moves.map((move) => (
          <MoveRow
            key={move.metric}
            move={move}
            open={open === move.metric}
            onToggle={() => onToggle(move.metric)}
          />
        ))
      )}
    </tbody>
  );
}

function MoveRow({
  move,
  open,
  onToggle,
}: {
  move: MetricMove;
  open: boolean;
  onToggle: () => void;
}) {
  const profile = profileOf(move.metric);
  const panelId = `drift-move-${move.metric}`;
  const relative = profile.rule === "magnitude" ? relativeMove(move) : null;

  return (
    <>
      <tr
        className={cn(
          "border-b border-border transition-colors duration-150",
          open ? "bg-surface-selected" : "hover:bg-surface-hover",
        )}
      >
        <td className="py-0 pl-4">
          <button
            type="button"
            aria-expanded={open}
            aria-controls={panelId}
            onClick={onToggle}
            className="flex size-6 items-center justify-center rounded-md text-muted-foreground transition-colors duration-150 hover:bg-surface-active hover:text-foreground focus-visible:focus-ring"
          >
            <ChevronRightIcon
              aria-hidden
              strokeWidth={2}
              className={cn(
                "size-3.5 transition-transform duration-150",
                open && "rotate-90",
              )}
            />
            <span className="sr-only">
              {open ? "Hide" : "Show"} what {profile.label} counts and the rule
              that decides whether its move is material
            </span>
          </button>
        </td>
        <td className="py-2.5 pr-4 text-xs font-medium sm:whitespace-nowrap">
          {profile.label}
          {move.material ? (
            <span className="sr-only"> — a material move</span>
          ) : null}
        </td>
        <td className="tnum py-2.5 pr-4 text-right text-xs text-muted-foreground">
          {formatMetricValue(profile.unit, move.before)}
        </td>
        <td className="tnum py-2.5 pr-4 text-right text-xs">
          {formatMetricValue(profile.unit, move.after)}
        </td>
        <td
          className={cn(
            "tnum py-2.5 pr-4 text-right text-xs",
            move.delta === 0
              ? "text-muted-foreground"
              : move.material
                ? "font-medium"
                : "",
          )}
        >
          {move.delta === 0 ? (
            <>
              <span aria-hidden>&mdash;</span>
              <span className="sr-only">no change</span>
            </>
          ) : (
            formatMetricDelta(profile.unit, move.delta)
          )}
        </td>
      </tr>

      <tr
        id={panelId}
        className={cn("border-b border-border", !open && "hidden")}
      >
        <td colSpan={5} className="px-4 py-5 sm:px-6">
          {open ? (
            <div className="grid max-w-[72rem] gap-6 lg:grid-cols-2">
              <div className="space-y-2">
                <p className="text-2xs font-medium text-muted-foreground">
                  What it counts
                </p>
                <p className="max-w-[62ch] text-xs leading-relaxed">
                  {profile.note}
                </p>
                <p className="font-mono text-2xs text-muted-foreground">
                  {move.metric}
                </p>
              </div>
              <div className="space-y-2">
                <p className="text-2xs font-medium text-muted-foreground">
                  {move.material
                    ? "Why this one is material"
                    : "The rule it was held to"}
                </p>
                <p className="max-w-[62ch] text-xs leading-relaxed">
                  {MATERIALITY_RULE_NOTE[profile.rule]}
                </p>
                {relative !== null ? (
                  <p className="text-2xs text-muted-foreground">
                    Relative to the baseline, this move is{" "}
                    <span className="tnum">
                      {relative > 0 ? "+" : "−"}
                      {formatRate(Math.abs(relative), 1)}
                    </span>
                    .
                  </p>
                ) : profile.rule === "magnitude" ? (
                  <p className="text-2xs text-muted-foreground">
                    The baseline is zero, so there is no ratio. Any appearance
                    from zero is material.
                  </p>
                ) : null}
              </div>
            </div>
          ) : null}
        </td>
      </tr>
    </>
  );
}

function Th({
  children,
  className,
}: {
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <th
      scope="col"
      className={cn("py-2.5 text-2xs font-medium text-muted-foreground", className)}
    >
      {children}
    </th>
  );
}

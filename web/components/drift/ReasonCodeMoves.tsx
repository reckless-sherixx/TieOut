"use client";

import { REASON_CODE_DESCRIPTION, REASON_CODE_LABEL } from "@/lib/labels";
import { cn } from "@/lib/utils";
import type { ReasonCode, ReasonCodeMove } from "@/lib/types";

const int = (n: number) => n.toLocaleString("en-IN");

/**
 * Which reason codes changed count, and which of them are new.
 *
 * ONLY THE CODES THAT MOVED ARE HERE, and that is the API's rule rather than a
 * filter applied on arrival: a code that fired ten times in both runs is not
 * drift. A code that STOPPED firing is reported too, with `appeared: false` —
 * "the orphan bank lines went away" is a finding in the same way their arrival
 * is one.
 *
 * `appeared` is the threshold spec §7 gives this half of the report: absent
 * before, present now. It is the shape of a new deduction type turning up
 * overnight, which is the example the whole drift feature is built around.
 *
 * `reason_code` arrives as a plain string, not the `ReasonCode` enum, because
 * the census is read out of a PERSISTED run and a run recorded before a code
 * existed must still be comparable. So an unrecognised code renders under its
 * own wire name instead of being dropped or crashing a lookup.
 */
export function ReasonCodeMoves({
  moves,
}: {
  moves: readonly ReasonCodeMove[];
}) {
  if (moves.length === 0) {
    return (
      <p className="max-w-[72ch] text-xs leading-relaxed text-muted-foreground">
        No reason code changed count between these two runs. The API reports a
        code only when its count moved, so an empty list here means the
        exception census is identical on both sides — every one of the eight
        codes fired exactly as often as before. That is a result rather than a
        missing section.
      </p>
    );
  }

  return (
    <div className="space-y-4">
      <div className="relative overflow-x-auto rounded-xl border border-border">
        <table className="w-full min-w-[38rem] border-collapse text-left">
          <caption className="sr-only">
            Reason codes whose count changed between the two runs
          </caption>
          <thead>
            <tr className="border-b border-border bg-surface">
              <th
                scope="col"
                className="w-56 py-2.5 pl-4 text-2xs font-medium text-muted-foreground"
              >
                Reason code
              </th>
              <th
                scope="col"
                className="py-2.5 text-2xs font-medium text-muted-foreground"
              >
                What it means
              </th>
              <th
                scope="col"
                className="w-24 py-2.5 text-right text-2xs font-medium text-muted-foreground"
              >
                Baseline
              </th>
              <th
                scope="col"
                className="w-24 py-2.5 pr-4 text-right text-2xs font-medium text-muted-foreground"
              >
                This run
              </th>
            </tr>
          </thead>
          <tbody>
            {moves.map((move) => {
              const known = move.reason_code in REASON_CODE_LABEL;
              const label = known
                ? REASON_CODE_LABEL[move.reason_code as ReasonCode]
                : move.reason_code;
              const description = known
                ? REASON_CODE_DESCRIPTION[move.reason_code as ReasonCode]
                : "This build does not carry a description for this code. It is shown under the name the API sent.";
              const gone = move.after === 0 && move.before > 0;

              return (
                <tr
                  key={move.reason_code}
                  className="border-b border-border last:border-b-0"
                >
                  <td className="py-2.5 pl-4">
                    <span className="inline-flex items-center gap-2">
                      <span
                        aria-hidden
                        className={cn(
                          "size-1.5 shrink-0 rounded-[2px]",
                          move.appeared
                            ? "bg-excepted"
                            : gone
                              ? "bg-matched"
                              : "bg-muted-foreground/50",
                        )}
                      />
                      <span className="text-xs font-medium">{label}</span>
                    </span>
                    <p className="mt-0.5 pl-[0.875rem] font-mono text-2xs text-muted-foreground">
                      {move.reason_code}
                      {/* The separator is a real character, not a margin. A
                          margin puts space on screen and nothing at all
                          between the two strings for a screen reader or a
                          copy-paste, which read them as one word. */}
                      {move.appeared ? (
                        <span> · new — absent before, present now</span>
                      ) : gone ? (
                        <span> · stopped firing</span>
                      ) : null}
                    </p>
                  </td>
                  <td className="py-2.5 pr-4 text-xs leading-relaxed text-muted-foreground">
                    {description}
                  </td>
                  <td className="tnum py-2.5 text-right text-xs text-muted-foreground">
                    {int(move.before)}
                  </td>
                  <td className="tnum py-2.5 pr-4 text-right text-xs font-medium">
                    {int(move.after)}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      <p className="max-w-[72ch] text-xs leading-relaxed text-muted-foreground">
        Codes whose count did not change are not listed, which is the API&apos;s
        rule and not a filter applied here: a code that fired the same number of
        times in both runs is not drift. A code that stopped firing is listed,
        because that is a finding too.
      </p>
    </div>
  );
}

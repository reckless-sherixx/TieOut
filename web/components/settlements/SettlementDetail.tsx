"use client";

import * as React from "react";
import { XIcon } from "lucide-react";
import { api } from "@/lib/api";
import { useResource } from "@/lib/hooks";
import { formatINR } from "@/lib/money";
import { TOLERANCE_PAISE } from "@/lib/tiers";
import { cn } from "@/lib/utils";
import { reconstructedNet, residualOf, type Settlement } from "@/lib/explorer";
import type { MatchDetail, RunState } from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Panel, PanelHeader } from "@/components/Panel";
import { LoadingBlock } from "@/components/States";
import { NettingDiagram } from "@/components/NettingDiagram";
import { CreditNarration, isCod } from "@/components/settlements/CreditNarration";
import { useRun } from "@/components/shell/RunScope";

/**
 * One settlement's netting, as the engine reconstructed it.
 *
 * The six money fields are laid out as the arithmetic they are — gross, four
 * deductions, and the total — and then the bank credit is set BESIDE that
 * total rather than substituted for it. On most rows the two are the same
 * number and the residual row reads as a dash. On a T3 row they are not, and
 * the whole reason this panel is built this way is so that the reader sees
 * which of the two they are looking at.
 *
 * MDR is charged on this settlement's OWN payment legs, so once a refund is in
 * the batch the fee is deliberately not 2.36% of the line above it. Real MDR
 * is not returned when a payment is refunded.
 *
 * THE DRAWING BELONGS TO THIS SELECTION AND TO NOTHING ELSE. The netting
 * diagram used to sit at the foot of the page with its own id field, which
 * meant two ways to look at one settlement that could disagree with each
 * other. It is driven by the open row now — and it is rendered only for a
 * batch that actually closed, because `GET /api/runs/{id}/batches/{id}`
 * requires a bank line and a tier and the contract says in as many words that
 * a batch that never closed is not reachable through it. Those are exactly the
 * rows this listing exists to surface, so the panel says what is missing
 * rather than requesting something that cannot answer.
 */
export function SettlementDetail({
  runId,
  runState,
  settlement,
  onClose,
}: {
  runId: string;
  /** Passed to the diagram, which keys its fetches on it. */
  runState: RunState;
  settlement: Settlement;
  onClose: () => void;
}) {
  // The run this panel lives inside, for the scorecard's ITC totals. Read from
  // the layout's context rather than fetched: `/runs/[id]` already holds one
  // RunSummary and six sibling routes polling their own copy is exactly the
  // duplication that context exists to prevent.
  const run = useRun();
  const panelRef = React.useRef<HTMLElement | null>(null);

  /**
   * OPENING A BREAKDOWN HAS TO BE VISIBLE FROM WHERE YOU CLICKED.
   *
   * This panel renders below a fifty-row table, and on a row past the first
   * few that put it 1,366 px beneath the trigger in a 900 px viewport: the
   * only feedback a click produced was `?setl=…` appearing in the address bar.
   * For a keyboard user it was worse — focus stayed on `Open` and the panel
   * was two dozen Shift+Tab stops away, with nothing announcing that anything
   * had happened at all.
   *
   * So the panel takes focus when it opens. It is programmatically focusable
   * and not in the tab order (`tabIndex={-1}`), which is the right shape for a
   * region a control sends you to rather than one you tab into, and it is
   * labelled by its own heading so a screen reader announces the settlement it
   * belongs to on arrival.
   *
   * The scroll is instant rather than smooth on purpose. Motion in this
   * console is 150–250 ms state feedback; animating 1,300 px of document is
   * neither, and it puts the thing you asked for off screen for the duration.
   */
  React.useEffect(() => {
    const panel = panelRef.current;
    if (!panel) return;
    panel.scrollIntoView({ block: "start", behavior: "auto" });
    panel.focus({ preventScroll: true });
  }, [settlement.settlement_id]);

  /**
   * Closing returns focus to the control that opened this, the same contract
   * the exception slide-over honours. The trigger is a row of a table that is
   * still on screen and still mounted, so it is found by the id it carries
   * rather than through a ref threaded down from the listing.
   */
  const close = React.useCallback(() => {
    const trigger = document.querySelector<HTMLElement>(
      `[data-settlement-trigger="${CSS.escape(settlement.settlement_id)}"]`,
    );
    onClose();
    trigger?.focus();
  }, [onClose, settlement.settlement_id]);

  const reconstructed = reconstructedNet(settlement);
  const residual = residualOf(settlement);
  const withinTolerance = Math.abs(residual) <= TOLERANCE_PAISE;
  const drawable = settlement.matched && settlement.bank_line_id !== null;

  return (
    // A fragment, not a wrapper: the two panels are siblings in the listing's
    // own vertical rhythm. Nesting the drawing inside the breakdown card would
    // be a card inside a card.
    <>
      <Panel
        id="settlement-breakdown"
        ref={panelRef}
        tabIndex={-1}
        aria-labelledby="settlement-breakdown-heading"
        className="scroll-mt-4 focus-visible:focus-ring"
      >
        <PanelHeader
          title={
            <span
              id="settlement-breakdown-heading"
              className="flex flex-wrap items-baseline gap-x-3 gap-y-1"
            >
              <span className="font-mono text-sm">{settlement.settlement_id}</span>
              {settlement.tier !== null ? (
                <span className="text-2xs font-normal text-muted-foreground">
                  closed at tier{" "}
                  <span className="font-mono text-foreground">
                    {settlement.tier}
                  </span>
                </span>
              ) : (
                <span className="inline-flex items-center rounded-none border border-excepted/60 px-1.5 py-px text-2xs font-medium">
                  Not closed
                </span>
              )}
            </span>
          }
          description={
            settlement.matched ? (
              <>
                Reconstructed from {settlement.payment_leg_count} payment{" "}
                {settlement.payment_leg_count === 1 ? "leg" : "legs"} and closed
                against one bank credit. Fee and tax legs are the batch&apos;s
                arithmetic rather than its size, so they are not counted here.
              </>
            ) : (
              <>
                Reconstructed from {settlement.payment_leg_count} payment{" "}
                {settlement.payment_leg_count === 1 ? "leg" : "legs"} and closed
                against nothing. The batch is intact; no bank line was found that
                it settles.
              </>
            )
          }
          action={
            <Button
              variant="ghost"
              size="icon-sm"
              aria-label={`Close the breakdown of ${settlement.settlement_id}`}
              onClick={close}
            >
              <XIcon aria-hidden strokeWidth={2} />
            </Button>
          }
        />

        <div className="grid gap-x-10 gap-y-8 border-t border-border px-6 py-6 lg:grid-cols-[minmax(0,26rem)_minmax(0,1fr)]">
          {/* No scroller here any more: the amounts below break between the
              formatted figure and the paise integer instead of being pushed
              out of the column intact. */}
          <div className="min-w-0">
            <Breakdown
              settlement={settlement}
              reconstructed={reconstructed}
              residual={residual}
            />
          </div>

          <div className="min-w-0 space-y-6">
            {/* THE SAME SIX NUMBERS, IN MERCHANT LANGUAGE (spec §6). It sits
                beside the table rather than replacing it: a controller wants
                the columns and a merchant wants the sentences, and the two are
                the same arithmetic because the narration calls the same
                `reconstructedNet` the table does. */}
            <CreditNarration settlement={settlement} metrics={run.metrics} />

            {residual !== 0 ? (
              <Residual
                residual={residual}
                reconstructed={reconstructed}
                net={settlement.net}
                withinTolerance={withinTolerance}
              />
            ) : (
              <p className="max-w-prose text-xs leading-relaxed text-muted-foreground">
                The reconstruction and the bank credit are the same number to the
                paise, so there is no residual to account for. That is the common
                case and not a guarantee: <code className="font-mono">net</code>{" "}
                is defined as the credit, and the tiers that use a tolerance can
                close a batch where the two differ.
              </p>
            )}

            <Facts settlement={settlement} />

            {settlement.match_id !== null ? (
              <Evidence runId={runId} matchId={settlement.match_id} />
            ) : null}
          </div>
        </div>
      </Panel>

      {drawable ? (
        <NettingDiagram
          runId={runId}
          runState={runState}
          settlementId={settlement.settlement_id}
        />
      ) : (
        <p className="max-w-prose px-1 text-xs leading-relaxed text-muted-foreground">
          There is no drawing for this batch.{" "}
          <code className="font-mono">
            GET /api/runs/{runId}/batches/{settlement.settlement_id}
          </code>{" "}
          returns a bank line and a tier as required fields, so it can only
          describe a batch that closed — and this one did not. The arithmetic
          above is the whole of what the run knows about it.
        </p>
      )}
    </>
  );
}

/* ------------------------------------------------------------------ *
 * The arithmetic
 * ------------------------------------------------------------------ */

function Breakdown({
  settlement: s,
  reconstructed,
  residual,
}: {
  settlement: Settlement;
  reconstructed: number;
  residual: number;
}) {
  /**
   * ONE NUMBER MUST NOT HAVE TWO NAMES ON ONE PANEL. `fees` on a COD
   * remittance is freight, COD handling and RTO -- not MDR -- and the
   * narration beside this table says so. A row labelled "MDR fees" against a
   * courier's charges would make the reader choose which of the two surfaces
   * to believe.
   */
  const cod = isCod(s);
  const deductions: [string, string, number][] = [
    [cod ? "Courier charges" : "MDR fees", "fees", s.fees],
    [cod ? "GST on courier charges" : "GST on MDR", "tax", s.tax],
    ["Refunds", "refunds", s.refunds],
    [cod ? "Withheld" : "Chargeback holds", "holds", s.holds],
  ];

  return (
    <table className="w-full border-collapse text-left">
      <caption className="sr-only">
        The netting of {s.settlement_id}: gross less four deductions, the
        reconstructed net, the bank credit, and the difference between them.
      </caption>
      <tbody>
        <Line
          label={cod ? "Collected" : "Gross"}
          field="gross"
          value={s.gross}
        />
        {deductions.map(([label, field, value]) => (
          <Line
            key={field}
            label={label}
            field={field}
            value={value}
            sign="−"
            muted={value === 0}
          />
        ))}
        <Line
          label="Reconstructed net"
          field="gross − fees − tax − refunds − holds"
          value={reconstructed}
          sign="="
          rule
          strong
        />
        <Line
          label="Bank credit"
          field="net"
          value={s.net}
          rule
          strong
          paise
        />
        {/* `reconstruction − net`, in that order, and `net` is the wire field
            for the bank credit. The engine's own evidence line for this row
            reads `residual delta=50 paise (net - credit)` and is quoted at the
            foot of this panel, so the two must agree in sign as well as in
            magnitude. They did not until this label and `residualOf` were
            settled on the engine's direction. */}
        <Line
          label="Residual"
          field="reconstruction − net"
          value={residual}
          zeroAsDash
          paise
        />
      </tbody>
    </table>
  );
}

function Line({
  label,
  field,
  value,
  sign,
  rule,
  strong,
  muted,
  paise,
  zeroAsDash,
}: {
  label: string;
  field: string;
  value: number;
  sign?: string;
  rule?: boolean;
  strong?: boolean;
  muted?: boolean;
  paise?: boolean;
  zeroAsDash?: boolean;
}) {
  const dash = zeroAsDash && value === 0;

  return (
    <tr className={cn(rule && "border-t border-border")}>
      <th
        scope="row"
        className={cn(
          "py-2 pr-4 text-left text-xs font-normal",
          strong ? "font-medium text-foreground" : "text-muted-foreground",
          muted && "text-muted-foreground",
        )}
      >
        {sign ? (
          <span aria-hidden className="mr-1.5 inline-block w-2 text-center">
            {sign}
          </span>
        ) : (
          <span aria-hidden className="mr-1.5 inline-block w-2" />
        )}
        {label}
      </th>
      <td className="hidden py-2 pr-4 font-mono text-2xs text-muted-foreground sm:table-cell">
        {field}
      </td>
      <td
        className={cn(
          // `tnum`, not `money`: `money` also sets `white-space: nowrap`, and
          // a figure carrying its paise integer beside it is wider than the
          // column at 375. The nowrap belongs on each of the two halves, not
          // on the pair — see below.
          "tnum py-2 text-right text-xs",
          strong && "font-medium",
          muted && "text-muted-foreground",
        )}
      >
        {dash ? (
          <>
            <span aria-hidden className="text-muted-foreground">
              &mdash;
            </span>
            <span className="sr-only">zero</span>
          </>
        ) : (
          <>
            {/* A rupee figure never breaks mid-number; the pair may break
                between its two halves. That is the whole difference between
                wrapping and garbling. */}
            <span className="money">{formatINR(value)}</span>
            {paise ? (
              // The integer on the wire beside the formatted value: money is
              // paise, and showing both makes that impossible to misread.
              <>
                {" "}
                <span className="ml-1 font-mono text-2xs font-normal text-muted-foreground">
                  <span className="money">· {value}</span> paise
                </span>
              </>
            ) : null}
          </>
        )}
      </td>
    </tr>
  );
}

/* ------------------------------------------------------------------ *
 * The residual, spelled out
 * ------------------------------------------------------------------ */

function Residual({
  residual,
  reconstructed,
  net,
  withinTolerance,
}: {
  residual: number;
  reconstructed: number;
  net: number;
  withinTolerance: boolean;
}) {
  return (
    <div className="rounded-lg border border-border bg-surface px-4 py-3.5">
      <p className="text-xs font-medium">
        The columns do not close, and that is the match
      </p>
      <p className="mt-2 max-w-prose text-xs leading-relaxed text-muted-foreground">
        <code className="font-mono">net</code> is the bank credit —{" "}
        <span className="money text-foreground">{formatINR(net)}</span> — not
        the sum of the five columns beside it, which reconstruct to{" "}
        <span className="money text-foreground">{formatINR(reconstructed)}</span>
        . The credit is{" "}
        <span className="money font-medium text-foreground">
          {formatINR(Math.abs(residual))}
        </span>{" "}
        {residual > 0 ? "below" : "above"} the reconstruction.{" "}
        {withinTolerance ? (
          <>
            That is inside the matcher&apos;s{" "}
            <span className="money">±{formatINR(TOLERANCE_PAISE)}</span>{" "}
            tolerance, which is the condition tier{" "}
            <span className="font-mono">T3</span> matches on: the reconstruction
            and the credit disagreed, and a tolerance rule accepted them
            anyway. Forcing the columns to agree here would describe a match
            that never happened.
          </>
        ) : (
          <>
            That is wider than the matcher&apos;s{" "}
            <span className="money">±{formatINR(TOLERANCE_PAISE)}</span>{" "}
            tolerance, which no deterministic tier closes on. Read the
            evidence below before trusting this row: either the batch was
            closed by a rule with a different bound, or these two numbers were
            not produced by the same reconstruction.
          </>
        )}
      </p>
    </div>
  );
}

/* ------------------------------------------------------------------ *
 * Identity
 * ------------------------------------------------------------------ */

function Facts({ settlement: s }: { settlement: Settlement }) {
  return (
    <dl className="grid grid-cols-[9rem_minmax(0,1fr)] gap-x-4 gap-y-2.5 text-xs">
      <dt className="text-muted-foreground">Payment legs</dt>
      <dd className="tnum">{s.payment_leg_count}</dd>

      <dt className="text-muted-foreground">Bank line</dt>
      <dd className="min-w-0 font-mono break-all">
        {s.bank_line_id ?? (
          <span className="font-sans text-muted-foreground">
            null
            <span className="text-2xs"> · no bank line closed this batch</span>
          </span>
        )}
      </dd>

      <dt className="text-muted-foreground">Match</dt>
      <dd className="min-w-0 font-mono break-all">
        {s.match_id ?? (
          <span className="font-sans text-muted-foreground">
            null
            <span className="text-2xs"> · this batch produced no match</span>
          </span>
        )}
      </dd>
    </dl>
  );
}

/* ------------------------------------------------------------------ *
 * The evidence, fetched for the row a reviewer opens
 * ------------------------------------------------------------------ */

/**
 * The match's evidence lines, and the bank line it closed against.
 *
 * The listing deliberately carries neither: it pages over every settlement of
 * the run, and both live on `GET /api/matches/{id}` for the one row a reviewer
 * opens. On a T3 row this is where the residual is spelled out by the engine
 * itself, in its own words.
 */
function Evidence({ runId, matchId }: { runId: string; matchId: string }) {
  const { data, error, loading } = useResource<MatchDetail>(
    `match:${runId}:${matchId}`,
    (signal) => api.getMatch(matchId, { signal }),
  );

  if (loading) {
    return (
      <LoadingBlock
        label={`Loading the evidence for match ${matchId}`}
        lines={3}
        className="max-w-md"
      />
    );
  }

  if (error || !data) {
    return (
      <p className="max-w-prose text-xs leading-relaxed text-muted-foreground">
        <span className="text-foreground">
          The evidence for this match did not load.
        </span>{" "}
        The breakdown above came from the settlements listing and is unaffected;
        what is missing is the engine&apos;s own account of why it closed, from{" "}
        <code className="font-mono">GET /api/matches/{matchId}</code>. Close and
        reopen this row to try again.
      </p>
    );
  }

  return (
    <div className="space-y-3">
      <h3 className="text-xs font-medium">Why the engine closed it</h3>
      {data.evidence.length === 0 ? (
        <p className="max-w-prose text-xs leading-relaxed text-muted-foreground">
          The match carries no evidence lines. Every rule that fires is supposed
          to write one, so an empty list here means the match was recorded
          without them rather than that it was made without rules.
        </p>
      ) : (
        <ul className="space-y-1.5">
          {data.evidence.map((line, i) => (
            <li
              key={`${i}-${line}`}
              className="max-w-prose text-xs leading-relaxed text-muted-foreground"
            >
              {line}
            </li>
          ))}
        </ul>
      )}
      <p className="tnum text-2xs text-muted-foreground">
        Confidence {data.confidence.toFixed(2)} · bank line{" "}
        <span className="font-mono">{data.subject.line_id}</span> ·{" "}
        {data.subject.credit !== null ? (
          <>
            credit{" "}
            <span className="money text-foreground">
              {formatINR(data.subject.credit)}
            </span>
          </>
        ) : (
          "no credit on the subject line"
        )}
      </p>
    </div>
  );
}

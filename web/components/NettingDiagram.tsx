"use client";

import * as React from "react";
import { api } from "@/lib/api";
import { useResource } from "@/lib/hooks";
import { formatINR } from "@/lib/money";
import { cn } from "@/lib/utils";
import { isTerminal } from "@/lib/labels";
import { TOLERANCE_PAISE } from "@/lib/tiers";
import { residualOf } from "@/lib/explorer";
import type { BatchNetting, PaginatedReconExceptions, RunState } from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Panel, PanelHeader } from "@/components/Panel";

/* ---------------------------------------------------------------- *
 * Geometry. One SVG, hand-rolled, no chart library and no dependency.
 * ---------------------------------------------------------------- */

const W = 1000;
const PAD = 28;
const COL_RIGHT = 190; // order bars are right-aligned here
const BAR_MIN = 20;
const BAR_MAX = 158;
const JOIN_X = 268; // where the fan converges
const BOX_X = 300;
const BOX_W = 380;
const BOX_RIGHT = BOX_X + BOX_W;
const BANK_X = 742;
const BANK_W = W - PAD - BANK_X;
const MAX_COL_H = 384;
const ROW_H = 27;

type Row = {
  label: string;
  value: number;
  kind: "gross" | "deduction" | "residual" | "net";
};

/**
 * THE COLUMNS DO NOT ALWAYS ADD UP, AND THAT IS THE CORRECT BEHAVIOUR.
 *
 * `net` on the wire is THE BANK CREDIT, not the reconstruction. T3 is precisely
 * the tier where those two differ — it accepts a batch whose reconstructed net
 * is within ±₹1.00 of the credit — so on a T3 batch
 *
 *     gross − fees − tax − refunds − holds  ≠  net
 *
 * by up to a rupee, and the exact residual is spelled out in the match's own
 * evidence lines. It reads like a rounding error and it is not one: it is the
 * tolerance rung doing its job.
 *
 * So this figure renders the residual as its own line and its own segment
 * rather than hiding it or normalising the columns until they close. A picture
 * that silently balances is telling a reviewer something false about how the
 * match was made.
 */
/* `residualOf` lives in `lib/explorer.ts` and is imported. It used to be
   duplicated here with the same magnitude and the OPPOSITE sign to the
   listing's copy, which is how one screen came to show −₹0.50 in a table and
   ₹0.50 in the drawing beside it. One definition, one direction. */

/**
 * N orders → one settlement batch → one bank line, with every deduction
 * annotated.
 *
 * The whole point of the picture: a bank statement shows one credit, and that
 * one number is dozens of orders minus four different deductions. It is drawn
 * by hand in SVG rather than by a chart library because no chart type fits —
 * this is a fan-in and a waterfall, not a series.
 *
 * Everything here is contained in this one file and mounted from one line of
 * the run page, so removing it costs nothing (it is cut item 1).
 *
 * TWO MODES, AND THE DIFFERENCE IS WHO OWNS THE SELECTION.
 *
 * `settlementId` supplied — a parent has already chosen a batch (a row in the
 * settlements listing) and this draws it. The component's own way in is not
 * rendered, because a second id field beside a selected row is a second answer
 * to a question the reader already answered, and the exception probe below is
 * not issued at all: nothing needs discovering.
 *
 * `settlementId` absent — the self-contained behaviour this component has
 * always had: discover ids from the run, offer them, accept a typed one.
 */
export function NettingDiagram({
  runId,
  runState,
  settlementId: controlled,
}: {
  runId: string;
  /** In the fetch key so a run that finishes re-runs settlement discovery. */
  runState: RunState;
  /**
   * The batch to draw, when the caller owns the selection. Optional: absent,
   * the component discovers and chooses one itself, which is what the run page
   * has always mounted it for.
   */
  settlementId?: string | null;
}) {
  const driven = controlled != null && controlled !== "";

  // CONTRACT NOTE: no operation lists a run's settlements or matches, so
  // there is no way to enumerate settlement ids from the contract. The ids
  // offered here are discovered from the settlement_id carried by PSP-
  // transaction subjects on the first page of exceptions — real wire data —
  // and the field is free text so any id can be inspected. Escalated in
  // LANE-E-REPORT.md.
  const { data: probe } = useResource<PaginatedReconExceptions>(
    `netting-probe:${runId}:${runState}`,
    (signal) => api.listRunExceptions(runId, { page: 1, size: 50 }, { signal }),
    !driven,
  );

  const discovered = React.useMemo(() => {
    const ids = new Set<string>();
    for (const row of probe?.items ?? []) {
      if (row.subject_type !== "psp_txn") continue;
      const settlementId = (row.subject as { settlement_id?: string | null })
        .settlement_id;
      if (settlementId) ids.add(settlementId);
    }
    return [...ids].sort();
  }, [probe]);

  // Until a settlement is chosen, follow whatever the run actually contains
  // rather than hard-coding an id that would 404 against a different dataset.
  const [chosen, setChosen] = React.useState<string | null>(null);
  const [draft, setDraft] = React.useState<string | null>(null);
  const settlementId = controlled ?? chosen ?? discovered[0] ?? "";

  const { data, error, loading } = useResource<BatchNetting>(
    `netting:${runId}:${settlementId}`,
    (signal) => api.getBatchNetting(runId, settlementId, { signal }),
    settlementId !== "",
  );

  function choose(id: string) {
    setDraft(id);
    setChosen(id);
  }

  return (
    <Panel>
      <PanelHeader
        title="Settlement netting"
        description="One bank credit is N orders minus four deductions. MDR is charged on this settlement's own payment legs only, so once a refund is in the batch the fee is deliberately not 2.36% of the line above it."
        action={
          // Not rendered when a parent owns the selection: an id field beside
          // an already-selected row is a second way to answer a question the
          // reader has answered, and the two can disagree on screen.
          driven ? null : (
            <form
              className="flex items-center gap-2"
              onSubmit={(e) => {
                e.preventDefault();
                const next = (draft ?? settlementId).trim();
                if (next) setChosen(next);
              }}
            >
              <Input
                value={draft ?? settlementId}
                onChange={(e) => setDraft(e.target.value)}
                aria-label="Settlement id"
                className="h-8 w-40 font-mono text-xs"
              />
              <Button type="submit" size="sm" variant="outline">
                Show
              </Button>
            </form>
          )
        }
      />

      {!driven && discovered.length > 0 ? (
        <div className="flex flex-wrap items-center gap-1.5 px-6 pb-4">
          <span className="text-2xs text-muted-foreground">In this run:</span>
          {discovered.map((id) => (
            <button
              key={id}
              type="button"
              onClick={() => choose(id)}
              className={cn(
                "rounded-md border border-border px-2 py-0.5 font-mono text-2xs transition-colors hover:bg-muted",
                // Selected reads as emphasis through its ground and border,
                // not through coloured text: --brand set on a 10% wash of
                // itself measures 4.30:1, under the floor. Same treatment as
                // the top bar's current destination and the reason filter's
                // selected code.
                id === settlementId &&
                  "border-brand/50 bg-surface-selected font-medium text-foreground",
              )}
            >
              {id}
            </button>
          ))}
        </div>
      ) : null}

      <div className="border-t border-border">
        {settlementId === "" ? (
          <p className="px-6 py-10 text-sm text-muted-foreground">
            {isTerminal(runState)
              ? "No settlement found in this run's exceptions. Enter a settlement id above to inspect one."
              : "This run is still executing. A settlement can be inspected once it finishes."}
          </p>
        ) : error ? (
          <p className="px-6 py-10 text-sm text-destructive" role="alert">
            Could not load settlement {settlementId}: {error.message}
          </p>
        ) : loading || !data ? (
          <p className="px-6 py-10 text-sm text-muted-foreground">
            Loading settlement…
          </p>
        ) : (
          <>
            <Diagram batch={data} />
            <DeductionStrip batch={data} />
            {data.evidence.length > 0 ? (
              <ul className="space-y-1 border-t border-border px-6 py-4">
                {data.evidence.map((line) => (
                  <li key={line} className="text-2xs text-muted-foreground">
                    {line}
                  </li>
                ))}
              </ul>
            ) : null}
          </>
        )}
      </div>
    </Panel>
  );
}

function Diagram({ batch }: { batch: BatchNetting }) {
  const orders = batch.orders;
  const n = orders.length;

  const gap = n > 32 ? 2 : n > 14 ? 5 : 10;
  const rawH = (MAX_COL_H - gap * Math.max(0, n - 1)) / Math.max(1, n);
  const tickH = Math.max(3, Math.min(26, rawH));
  const colH = n * tickH + Math.max(0, n - 1) * gap;

  const rows: Row[] = [
    { label: "Gross", value: batch.gross, kind: "gross" },
    { label: "MDR 2.36%", value: batch.fees, kind: "deduction" },
    { label: "GST 18% on MDR", value: batch.tax, kind: "deduction" },
  ];
  if (batch.refunds > 0)
    rows.push({ label: "Refunds", value: batch.refunds, kind: "deduction" });
  if (batch.holds > 0)
    rows.push({ label: "Holds", value: batch.holds, kind: "deduction" });
  const residual = residualOf(batch);
  if (residual !== 0)
    rows.push({ label: "Residual", value: residual, kind: "residual" });
  rows.push({ label: "Net", value: batch.net, kind: "net" });

  const boxH = 40 + rows.length * ROW_H + 14;
  const contentH = Math.max(colH, boxH, 150);
  const top = PAD + 32;
  const H = top + contentH + PAD + 22;
  const cy = top + contentH / 2;

  const colTop = top + (contentH - colH) / 2;
  const boxTop = top + (contentH - boxH) / 2;

  const maxGross = Math.max(...orders.map((o) => o.gross_amount), 1);
  const barFor = (amount: number) =>
    BAR_MIN + (amount / maxGross) * (BAR_MAX - BAR_MIN);

  const showTicks = n <= 120; // beyond that the fan is a smear, not a picture

  return (
    <div className="overflow-x-auto px-6 py-6">
      <svg
        viewBox={`0 0 ${W} ${H}`}
        width="100%"
        className="min-w-[46rem]"
        role="img"
        aria-label={`${n} orders settle as batch ${batch.settlement_id} and arrive as one bank credit of ${formatINR(batch.net)} on line ${batch.bank_line_id}`}
        style={{ fontVariantNumeric: "tabular-nums" }}
      >
        {/* ---- Zone A: the orders ---- */}
        <text
          x={COL_RIGHT}
          y={PAD + 4}
          textAnchor="end"
          className="fill-muted-foreground"
          fontSize="10"
          letterSpacing="1.4"
        >
          {n === 1 ? "1 ORDER" : `${n} ORDERS`}
        </text>

        {showTicks &&
          orders.map((order, i) => {
            const y = colTop + i * (tickH + gap);
            const w = barFor(order.gross_amount);
            return (
              <rect
                key={order.order_id + i}
                x={COL_RIGHT - w}
                y={y}
                width={w}
                height={tickH}
                rx={Math.min(3, tickH / 2)}
                className="fill-brand/70"
              >
                <title>
                  {order.order_id} · {formatINR(order.gross_amount)}
                </title>
              </rect>
            );
          })}

        <text
          x={COL_RIGHT}
          y={colTop + colH + 18}
          textAnchor="end"
          className="fill-muted-foreground"
          fontSize="11"
        >
          {formatINR(batch.gross)} gross
        </text>

        {/* ---- Zone B: the fan-in ---- */}
        {showTicks &&
          orders.map((order, i) => {
            const y = colTop + i * (tickH + gap) + tickH / 2;
            return (
              <path
                key={`fan-${order.order_id}-${i}`}
                d={`M ${COL_RIGHT + 3} ${y} C ${COL_RIGHT + 44} ${y}, ${JOIN_X - 44} ${cy}, ${JOIN_X} ${cy}`}
                fill="none"
                className="stroke-brand/35"
                strokeWidth={n > 32 ? 0.8 : 1.2}
              />
            );
          })}

        <circle cx={JOIN_X} cy={cy} r="3.5" className="fill-brand" />
        <line
          x1={JOIN_X}
          y1={cy}
          x2={BOX_X - 8}
          y2={cy}
          className="stroke-brand"
          strokeWidth="1.5"
        />
        <Arrow x={BOX_X - 8} y={cy} />

        {/* ---- Zone C: the settlement batch ---- */}
        <rect
          x={BOX_X}
          y={boxTop}
          width={BOX_W}
          height={boxH}
          rx="10"
          className="fill-transparent stroke-border"
          strokeWidth="1"
        />
        <text
          x={BOX_X + 18}
          y={boxTop + 22}
          className="fill-muted-foreground"
          fontSize="10"
          letterSpacing="1.4"
        >
          SETTLEMENT
        </text>
        <text
          x={BOX_RIGHT - 18}
          y={boxTop + 22}
          textAnchor="end"
          className="fill-foreground font-mono"
          fontSize="11"
        >
          {batch.settlement_id}
        </text>

        {rows.map((row, i) => {
          const y = boxTop + 40 + i * ROW_H + ROW_H / 2 + 4;
          const isNet = row.kind === "net";
          const isGross = row.kind === "gross";
          return (
            <g key={row.label}>
              {isNet ? (
                <line
                  x1={BOX_X + 18}
                  y1={y - 17}
                  x2={BOX_RIGHT - 18}
                  y2={y - 17}
                  className="stroke-border"
                  strokeWidth="1"
                />
              ) : null}
              <text
                x={BOX_X + 18}
                y={y}
                className={
                  isNet || isGross || row.kind === "residual"
                    ? "fill-foreground"
                    : "fill-muted-foreground"
                }
                fontSize="12"
                fontWeight={isNet ? 600 : 400}
              >
                {row.kind === "deduction" || row.kind === "residual"
                  ? "− "
                  : isNet
                    ? "= "
                    : ""}
                {row.label}
              </text>
              <text
                x={BOX_RIGHT - 18}
                y={y}
                textAnchor="end"
                className={
                  isNet
                    ? "fill-brand"
                    : isGross || row.kind === "residual"
                      ? "fill-foreground"
                      : "fill-muted-foreground"
                }
                fontSize="12"
                fontWeight={isNet ? 600 : 400}
              >
                {formatINR(row.value)}
              </text>
            </g>
          );
        })}

        {/* ---- Zone D: the one bank line ---- */}
        <line
          x1={BOX_RIGHT}
          y1={cy}
          x2={BANK_X - 8}
          y2={cy}
          className="stroke-brand"
          strokeWidth="1.5"
        />
        <Arrow x={BANK_X - 8} y={cy} />

        <text
          x={BANK_X}
          y={PAD + 4}
          className="fill-muted-foreground"
          fontSize="10"
          letterSpacing="1.4"
        >
          BANK STATEMENT
        </text>
        <rect
          x={BANK_X}
          y={cy - 44}
          width={BANK_W}
          height="88"
          rx="10"
          className="fill-brand/10 stroke-brand/40"
          strokeWidth="1"
        />
        <text
          x={BANK_X + 16}
          y={cy - 20}
          className="fill-muted-foreground font-mono"
          fontSize="10"
        >
          {batch.bank_line_id}
        </text>
        <text
          x={BANK_X + 16}
          y={cy + 8}
          className="fill-foreground"
          fontSize="17"
          fontWeight={600}
        >
          {formatINR(batch.net)}
        </text>
        <text
          x={BANK_X + 16}
          y={cy + 28}
          className="fill-muted-foreground"
          fontSize="10"
        >
          one credit · tier {batch.tier}
        </text>
      </svg>
    </div>
  );
}

function Arrow({ x, y }: { x: number; y: number }) {
  return (
    <path
      d={`M ${x} ${y - 4.5} L ${x + 8} ${y} L ${x} ${y + 4.5} Z`}
      className="fill-brand"
    />
  );
}

/**
 * Where the gross went, as a proportional CSS strip. The segments are the
 * arithmetic rather than a decoration, so when the arithmetic does not close
 * the residual is one of them.
 *
 * The denominator is the total of what is DRAWN, not `gross`: on a T3 batch the
 * two are not the same number, and dividing by gross would either leave a gap
 * the strip does not explain or overflow the track and clip the difference out
 * of sight. Either way the picture would be balancing itself.
 */
function DeductionStrip({ batch }: { batch: BatchNetting }) {
  const residual = residualOf(batch);
  const parts = [
    { key: "net", label: "Net", value: batch.net, className: "bg-brand" },
    { key: "fees", label: "MDR", value: batch.fees, className: "bg-brand/60" },
    { key: "tax", label: "GST", value: batch.tax, className: "bg-brand/40" },
    {
      key: "refunds",
      label: "Refunds",
      value: batch.refunds,
      className: "bg-brand/25",
    },
    { key: "holds", label: "Holds", value: batch.holds, className: "bg-brand/15" },
    {
      key: "residual",
      label: residual > 0 ? "Residual" : "Residual (credit over)",
      value: Math.abs(residual),
      className: "bg-excepted",
    },
  ].filter((p) => p.value > 0);

  const drawn = parts.reduce((sum, p) => sum + p.value, 0) || 1;

  return (
    <div className="space-y-3 border-t border-border px-6 py-5">
      <div className="flex h-2.5 w-full overflow-hidden rounded-none bg-muted">
        {parts.map((part) => (
          <div
            key={part.key}
            className={part.className}
            style={{ width: `${(part.value / drawn) * 100}%` }}
            title={`${part.label} ${formatINR(part.value)}`}
          />
        ))}
      </div>

      {residual !== 0 ? (
        <p className="max-w-[72ch] text-2xs leading-relaxed text-muted-foreground">
          <span className="font-medium text-foreground">
            These columns do not add up, by {formatINR(Math.abs(residual))}, and
            that is the tolerance rung working.
          </span>{" "}
          <code className="font-mono">net</code> on the wire is the bank credit,
          not the reconstruction, and tier {batch.tier} accepts a batch whose
          reconstructed net differs from the credit within ±
          {formatINR(TOLERANCE_PAISE)}. The exact residual is in the match evidence below.
          Forcing these figures to close would be reporting a match that was
          never made that way.
        </p>
      ) : null}
      <dl className="flex flex-wrap gap-x-8 gap-y-2">
        {parts.map((part) => (
          <div key={part.key} className="flex items-start gap-2">
            <span
              aria-hidden
              className={`mt-1.5 size-2 shrink-0 rounded-[3px] ${part.className}`}
            />
            <div>
              <dt className="text-2xs text-muted-foreground">{part.label}</dt>
              <dd className="money text-xs font-medium">
                {formatINR(part.value)}
              </dd>
            </div>
          </div>
        ))}
      </dl>
    </div>
  );
}

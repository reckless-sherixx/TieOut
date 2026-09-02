"use client";

import * as React from "react";
import { formatINR } from "@/lib/money";
import { reconstructedNet, residualOf, type Settlement } from "@/lib/explorer";
import type { Metrics } from "@/lib/types";
import { cn } from "@/lib/utils";

const int = (n: number) => n.toLocaleString("en-IN");

/**
 * "Why is my bank credit less than my dashboard?" — spec 2026-08-30 §6.
 *
 * The netting table beside this is the arithmetic; this is the same numbers as
 * a paragraph a merchant can read without knowing what a settlement leg is.
 * ONE SENTENCE PER DEDUCTION, each carrying the rupee figure the engine
 * produced, in Indian grouping, in the order money actually leaves: gross
 * captured, MDR, GST on MDR, refunds netted, chargeback holds, credit.
 *
 * **No new arithmetic, and that is not a limitation.** Every figure below is a
 * field of the settlement row already on screen; the only thing computed here
 * is `gross − fees − tax − refunds − holds`, which is `reconstructedNet` — the
 * same function the table above it uses, so the prose and the columns cannot
 * disagree. A percentage is shown only where it is exactly a ratio of two wire
 * integers, never as a rate this component decided.
 *
 * **A deduction of zero is not mentioned.** "Refunds: ₹0.00" in a narrative is
 * noise that pushes the sentences that matter off the screen; the table beside
 * it still shows every line including the zeroes, so nothing is hidden — one
 * surface is exhaustive and the other is readable, deliberately.
 *
 * **COD remittances get courier language.** A settlement id beginning
 * `setl_cod` is a Delhivery remittance, where the same six fields mean
 * collections, freight, COD handling, RTO and GST rather than captures and
 * MDR. Using PSP words for it would describe the wrong business event, and the
 * COD gap is the one no incumbent names.
 */
export function CreditNarration({
  settlement,
  metrics,
}: {
  settlement: Settlement;
  /** The run's scorecard, or null — an upload run has none. See `ItcNote`. */
  metrics: Metrics | null;
}) {
  const cod = isCod(settlement);
  const reconstructed = reconstructedNet(settlement);
  const residual = residualOf(settlement);

  const lines = cod
    ? codLines(settlement)
    : pspLines(settlement);

  return (
    <section
      aria-labelledby="credit-narration-heading"
      className="rounded-lg border border-border bg-surface px-5 py-4"
    >
      {/* AN UNMATCHED BATCH HAS NO CREDIT TO ASK ABOUT. The heading used to
          ask why the bank credit was lower on a settlement where no bank
          credit was found at all, which is a question about a number that
          does not exist. */}
      <h3 id="credit-narration-heading" className="text-xs font-medium">
        {settlement.matched
          ? cod
            ? "Why did the courier remit less than they collected?"
            : "Why is the bank credit less than what you captured?"
          : cod
            ? "What this remittance should have paid you"
            : "What this settlement should have paid you"}
      </h3>

      <ol className="mt-3 space-y-2.5">
        {lines.map((line) => (
          <li
            key={line.key}
            className="grid grid-cols-[minmax(0,1fr)_auto] items-baseline gap-x-4 text-xs leading-relaxed"
          >
            <span
              className={cn(
                line.emphasis ? "text-foreground" : "text-muted-foreground",
              )}
            >
              {line.text}
            </span>
            <span
              className={cn(
                "money tnum text-right",
                line.emphasis ? "font-medium" : "text-muted-foreground",
              )}
            >
              {line.sign}
              {formatINR(line.amount)}
            </span>
          </li>
        ))}
      </ol>

      <p className="mt-3.5 border-t border-border pt-3 text-xs leading-relaxed">
        {settlement.matched ? (
          <>
            <span className="text-muted-foreground">
              {cod
                ? "The courier remitted "
                : "The bank credited your account "}
            </span>
            <span className="money font-medium">{formatINR(settlement.net)}</span>
            <span className="text-muted-foreground">
              {" "}
              against this {cod ? "remittance" : "settlement"}
              {residual === 0 ? (
                <>
                  , which is exactly the figure above.
                </>
              ) : (
                <>
                  {" "}
                  — <span className="money">{formatINR(Math.abs(residual))}</span>{" "}
                  {residual > 0 ? "less than" : "more than"} the{" "}
                  <span className="money">{formatINR(reconstructed)}</span> these
                  deductions account for. That gap is not explained by anything
                  on this page; the engine&apos;s own evidence for the match is
                  below.
                </>
              )}
            </span>
          </>
        ) : (
          <span className="text-muted-foreground">
            <span className="text-foreground">
              Nothing in the bank statement closes this{" "}
              {cod ? "remittance" : "batch"}.
            </span>{" "}
            The deductions above are what the {cod ? "courier" : "PSP"} reported,
            so this is what you are owed —{" "}
            <span className="money text-foreground">
              {formatINR(reconstructed)}
            </span>{" "}
            — and no credit was found for it. Chase this one.
          </span>
        )}
      </p>

      <ItcNote settlement={settlement} metrics={metrics} cod={cod} />
    </section>
  );
}

/* ------------------------------------------------------------------ *
 * The sentences
 * ------------------------------------------------------------------ */

type Line = {
  key: string;
  text: React.ReactNode;
  amount: number;
  sign: string;
  emphasis?: boolean;
};

function pspLines(s: Settlement): Line[] {
  const lines: Line[] = [
    {
      key: "gross",
      emphasis: true,
      sign: "",
      amount: s.gross,
      text: (
        <>
          You captured this much across{" "}
          <span className="tnum">{int(s.payment_leg_count)}</span>{" "}
          {s.payment_leg_count === 1 ? "payment" : "payments"} in this batch.
        </>
      ),
    },
  ];

  if (s.fees !== 0) {
    lines.push({
      key: "fees",
      sign: "−",
      amount: s.fees,
      text: (
        <>
          Razorpay&apos;s MDR on those payments.
          {/* The caveat belongs only to a batch that HAS a refund in it. On
              the common row it explained an arithmetic surprise that was not
              on the screen. */}
          {s.refunds !== 0 ? (
            <>
              {" "}
              It is charged on the payments, not on the line above: real MDR is
              not returned when a payment is refunded, so this is deliberately
              not a percentage of a gross that a refund has already reduced.
            </>
          ) : null}
        </>
      ),
    });
  }

  if (s.tax !== 0) {
    lines.push({
      key: "tax",
      sign: "−",
      amount: s.tax,
      text: (
        <>
          GST on that MDR
          {s.fees !== 0 ? (
            <>
              {" "}
              — <span className="tnum">{gstRate(s.tax, s.fees)}</span> of the
              fee
            </>
          ) : null}
          . This is a tax you paid, and it is input tax credit you can claim
          back.
        </>
      ),
    });
  }

  if (s.refunds !== 0) {
    lines.push({
      key: "refunds",
      sign: "−",
      amount: s.refunds,
      text: (
        <>
          Refunds that settled in this batch, netted off before the payout. The
          MDR on the original payments is not returned with them.
        </>
      ),
    });
  }

  if (s.holds !== 0) {
    lines.push({
      key: "holds",
      sign: "−",
      amount: s.holds,
      text: (
        <>
          Held against chargebacks. This is your money, withheld pending the
          dispute rather than deducted — it returns to a later payout if the
          dispute goes your way.
        </>
      ),
    });
  }

  return lines;
}

function codLines(s: Settlement): Line[] {
  const lines: Line[] = [
    {
      key: "gross",
      emphasis: true,
      sign: "",
      amount: s.gross,
      text: (
        <>
          Cash the courier collected from{" "}
          <span className="tnum">{int(s.payment_leg_count)}</span>{" "}
          {s.payment_leg_count === 1 ? "delivery" : "deliveries"} in this
          remittance.
        </>
      ),
    },
  ];

  if (s.fees !== 0) {
    lines.push({
      key: "fees",
      sign: "−",
      amount: s.fees,
      text: (
        <>
          Courier charges: freight, COD handling and RTO on returned shipments.
          The remittance file lists those three separately; they reach the
          engine as one deduction, so this figure is their total and not any one
          of them.
        </>
      ),
    });
  }

  if (s.tax !== 0) {
    lines.push({
      key: "tax",
      sign: "−",
      amount: s.tax,
      text: (
        <>
          GST on those courier charges
          {s.fees !== 0 ? (
            <>
              {" "}
              — <span className="tnum">{gstRate(s.tax, s.fees)}</span> of them
            </>
          ) : null}
          . Input tax credit in exactly the way GST on MDR is.
        </>
      ),
    });
  }

  if (s.refunds !== 0) {
    lines.push({
      key: "refunds",
      sign: "−",
      amount: s.refunds,
      text: <>Refunds netted against this remittance.</>,
    });
  }

  if (s.holds !== 0) {
    lines.push({
      key: "holds",
      sign: "−",
      amount: s.holds,
      text: <>Withheld by the courier rather than remitted.</>,
    });
  }

  return lines;
}

/* ------------------------------------------------------------------ *
 * The ITC linkage
 * ------------------------------------------------------------------ */

/**
 * Whether this settlement's GST is claimable, and what it turns on.
 *
 * **The rule is the engine's, not this component's:** only a settlement the
 * engine could close substantiates its GST, because a tax credit needs the
 * transaction behind it evidenced. So a matched batch's `tax` is part of the
 * run's `itc_substantiated_paise` and an unmatched batch's is part of
 * `itc_at_risk_paise` — and this says which side of that line the row in front
 * of the reader falls on, then names the run-level figure it contributes to.
 *
 * The run total is shown only when there is one. An upload run has no
 * scorecard at all (`Metrics` is null), and the per-settlement statement is
 * still true and still worth making without it.
 */
function ItcNote({
  settlement,
  metrics,
  cod,
}: {
  settlement: Settlement;
  metrics: Metrics | null;
  cod: boolean;
}) {
  if (settlement.tax === 0) return null;

  return (
    <p className="mt-3 text-xs leading-relaxed text-muted-foreground">
      {settlement.matched ? (
        <>
          <span className="text-foreground">
            The <span className="money">{formatINR(settlement.tax)}</span> of GST
            here is substantiated.
          </span>{" "}
          This {cod ? "remittance" : "settlement"} closed against a{" "}
          {cod ? "remittance credit" : "bank credit"}, so the transaction behind
          the tax is evidenced and the credit is claimable.
        </>
      ) : (
        <>
          <span className="text-excepted-fg">
            The <span className="money">{formatINR(settlement.tax)}</span> of GST
            here is at risk.
          </span>{" "}
          Nothing in the statement closes this{" "}
          {cod ? "remittance" : "settlement"}, so the transaction behind the tax
          is not evidenced — claiming it is a position you would have to defend
          without a matching credit.
        </>
      )}
      {metrics ? (
        <>
          {" "}
          Across the whole run,{" "}
          <span className="money">
            {formatINR(metrics.itc_substantiated_paise)}
          </span>{" "}
          is substantiated and{" "}
          <span className="money">{formatINR(metrics.itc_at_risk_paise)}</span>{" "}
          is at risk.
        </>
      ) : null}
    </p>
  );
}

/* ------------------------------------------------------------------ *
 * Helpers
 * ------------------------------------------------------------------ */

/** A settlement whose id begins `setl_cod` is a courier remittance. */
export function isCod(settlement: Settlement): boolean {
  return settlement.settlement_id.startsWith("setl_cod");
}

/**
 * `tax ÷ fees`, to one decimal place.
 *
 * An exact ratio of two integers the engine emitted, not a rate this component
 * knows: if a PSP ever charged GST at a different rate, this would show that
 * rate rather than the 18% everyone expects, which is the entire reason it is
 * divided rather than written down.
 */
function gstRate(tax: number, fees: number): string {
  return `${((tax / fees) * 100).toFixed(1)}%`;
}

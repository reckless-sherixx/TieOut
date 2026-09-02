import { formatINR } from "@/lib/money";
import { SUBJECT_TYPE_LABEL } from "@/lib/labels";
import { tagSubject, type SubjectRecord, type SubjectType } from "@/lib/types";

/**
 * The subject record an exception is about, rendered by its declared shape.
 *
 * The shape is chosen by the sibling `subject_type` tag, never by sniffing for
 * a field: `SubjectRecord` is a bare union of three input shapes with no
 * discriminator inside the records themselves.
 */
export function SubjectRecordView({
  subjectType,
  subject,
}: {
  subjectType: SubjectType;
  subject: SubjectRecord;
}) {
  const tagged = tagSubject(subjectType, subject);

  switch (tagged.type) {
    case "order": {
      const o = tagged.record;
      return (
        <Fields
          title={SUBJECT_TYPE_LABEL.order}
          rows={[
            ["Order id", <Mono key="id">{o.order_id}</Mono>],
            ["Order date", o.order_date],
            ["Customer", <Mono key="c">{o.customer_ref}</Mono>],
            ["Gross", <Money key="g" paise={o.gross_amount} />],
            ["Currency", o.currency],
            ["Status", o.status],
          ]}
        />
      );
    }
    case "psp_txn": {
      const t = tagged.record;
      return (
        <Fields
          title={SUBJECT_TYPE_LABEL.psp_txn}
          rows={[
            ["Transaction id", <Mono key="id">{t.txn_id}</Mono>],
            ["Type", t.txn_type],
            [
              "Order id",
              t.order_id ? (
                <Mono key="o">{t.order_id}</Mono>
              ) : (
                <Absent key="o" note="missing order reference" />
              ),
            ],
            ["Captured at", <Mono key="ca">{t.captured_at}</Mono>],
            [
              // `amount` is SIGNED from the merchant's point of view. Do not
              // conflate it with a bank line's unsigned credit/debit.
              "Amount (signed)",
              <Money key="a" paise={t.amount} />,
            ],
            [
              "Settlement",
              t.settlement_id ? (
                <Mono key="s">{t.settlement_id}</Mono>
              ) : (
                <Absent key="s" note="in no settlement batch" />
              ),
            ],
            [
              "Settled at",
              t.settled_at ?? <Absent key="sa" note="not settled" />,
            ],
          ]}
        />
      );
    }
    case "bank_line": {
      const b = tagged.record;
      return (
        <Fields
          title={SUBJECT_TYPE_LABEL.bank_line}
          rows={[
            ["Line id", <Mono key="id">{b.line_id}</Mono>],
            ["Date", b.txn_date],
            [
              "Narration",
              // Rendered verbatim, double spaces and all. The garbling IS the
              // data; normalising it for display erases the defect.
              <span
                key="n"
                className="block whitespace-pre-wrap break-words rounded-md border border-border bg-muted/50 px-2 py-1.5 font-mono text-xs"
              >
                {b.narration}
              </span>,
            ],
            [
              "Credit",
              b.credit !== null ? (
                <Money key="c" paise={b.credit} />
              ) : (
                <Absent key="c" note="debit line" />
              ),
            ],
            [
              "Debit",
              b.debit !== null ? (
                <Money key="d" paise={b.debit} />
              ) : (
                <Absent key="d" note="credit line" />
              ),
            ],
            ["Balance", <Money key="b" paise={b.balance} />],
            [
              "UTR",
              b.utr ? <Mono key="u">{b.utr}</Mono> : <Absent key="u" note="absent" />,
            ],
          ]}
        />
      );
    }
  }
}

function Fields({
  title,
  rows,
}: {
  title: string;
  rows: [string, React.ReactNode][];
}) {
  return (
    <div>
      <p className="text-2xs font-medium uppercase tracking-[0.12em] text-muted-foreground">
        {title}
      </p>
      {/* `minmax(0,1fr)`, not `1fr`: a bare `1fr` floors the value track at its
          content's min-content width, so one long field would push the panel
          wider than its container instead of wrapping inside it. The label
          column gives up 3rem below `sm` so the value column stays readable at
          375 — that is a wrap BETWEEN fields, which is fine; the amounts
          themselves still never break mid-number. */}
      <dl className="mt-3 grid grid-cols-[7rem_minmax(0,1fr)] gap-x-4 gap-y-2.5 text-xs sm:grid-cols-[10rem_minmax(0,1fr)]">
        {rows.map(([label, value]) => (
          <div key={label} className="contents">
            <dt className="text-muted-foreground">{label}</dt>
            <dd className="min-w-0">{value}</dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

function Mono({ children }: { children: React.ReactNode }) {
  return <span className="font-mono text-xs break-all">{children}</span>;
}

/**
 * A formatted amount and the integer paise it was formatted from.
 *
 * THE `money` UTILITY SETS `white-space: nowrap`, AND IT BELONGS ON EACH HALF
 * OF THIS PAIR RATHER THAN ON THE PAIR. Set on the wrapper it made
 * `₹9,21,652.07 · 92165207 paise` a single unbreakable run about 175px wide,
 * which does not fit the value column of this panel at 375 and took the whole
 * page into horizontal scroll — the reason two callers had wrapped this
 * component in a scroller of their own.
 *
 * The distinction that matters: a rupee figure must never break mid-number,
 * because half of `₹9,21,652.07` on one line reads as a different amount. The
 * gap between the figure and its paise integer is not inside a number, so it
 * is a legitimate place to wrap.
 */
function Money({ paise }: { paise: number }) {
  return (
    <span className="font-medium">
      <span className="money">{formatINR(paise)}</span>{" "}
      {/* The integer on the wire, alongside the formatted value: money is
          paise, and showing both makes that impossible to misread. The unit
          can fall to the next line; the integer cannot break away from its
          separator, and cannot break inside itself. */}
      <span className="ml-1 font-mono text-2xs font-normal text-muted-foreground">
        <span className="money">· {paise}</span> paise
      </span>
    </span>
  );
}

/** A legitimately null field. Nullability here is load-bearing, not an error. */
function Absent({ note }: { note: string }) {
  return (
    <span className="text-muted-foreground">
      null <span className="text-2xs">· {note}</span>
    </span>
  );
}

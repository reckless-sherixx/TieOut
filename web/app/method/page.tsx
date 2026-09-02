import type { Metadata } from "next";
import { MethodNav } from "@/components/method/MethodNav";
import {
  DEFECT_CLASSES,
  LIMITATIONS,
  PIPELINE,
  SECTIONS,
  TIERS,
  VERIFIER_CHECKS_DETAIL,
} from "@/components/method/content";

export const metadata: Metadata = {
  title: "Method — Tieout",
  description:
    "How the reconciliation engine works: the pipeline, the tier ladder, the verifier's six checks, the ten defect classes, and the limitations of the result.",
};

/**
 * The page that lets someone understand the system without cloning the repo.
 *
 * Read mode, not Operate mode: one column, a measure of about 70 characters,
 * and the reasoning in full sentences. Everything that is a claim about a
 * number is stated with the mechanism that produces it, because the whole
 * argument of this project is that a number you cannot check is not a result.
 */
export default function MethodPage() {
  return (
    <div className="mx-auto w-full max-w-[92rem] px-6 py-12 lg:px-8">
      <div className="lg:grid lg:grid-cols-[minmax(0,1fr)_13rem] lg:items-start lg:gap-16">
        {/* The measure is set in rem, not in `ch`: `ch` resolves against the
            element's OWN font-size, and this element is 16px while its prose
            is 15px — so `70ch` here rendered as 77 characters of body text.
            41.5rem measures 69 characters at the paragraph's own size. */}
        <article className="min-w-0 max-w-[41.5rem]">
          <h1 className="text-2xl font-medium tracking-tight text-balance">
            How this reconciles a merchant&apos;s books
          </h1>
          <p className="mt-5 text-base leading-relaxed">
            Three sources go in — a merchant&apos;s sales register, a payment
            processor&apos;s settlement report, and the bank statement — and a
            match rate, a false-match rate and an itemised exception list come
            out. This page is the mechanism behind those three numbers, in
            enough detail to argue with.
          </p>

          <Section id="shape" title="Why this is not a VLOOKUP">
            <P>
              The naive framing is &ldquo;match invoice A to payment B&rdquo;.
              That is a lookup, and it is not the problem. The real shape is
              many-to-one with deductions: N orders collapse into one bank
              credit, net of the processor&apos;s discount rate, net of GST
              charged on that discount rate, net of refunds belonging to a
              previous settlement cycle, net of chargeback holds. The bank line
              carries none of the components — only the residue.
            </P>
            <Figure caption="One settlement carrying all four deduction classes at once. The bank credit is the last line, and it is the only figure the statement shows.">
              {`  payment legs   ORD-004510        +21,00,000 paise    ₹21,000.00
                 ORD-004511         +6,75,000          ₹ 6,750.00
                                   ───────────
  payment gross                     27,75,000          ₹27,750.00

− refund         rfnd_2001           8,90,000          ₹ 8,900.00   cycle N−1
− chargeback     cb_7701               50,000          ₹   500.00   order not in register
− MDR   2.36% of 27,75,000             65,490          ₹   654.90
− GST   18%   of     65,490            11,788          ₹   117.88
                                   ───────────
  net                               17,57,722          ₹17,577.22
  bank credit BL-0002               17,57,722          ₹17,577.22   exact`}
            </Figure>
            <P>
              Four things in that example are the whole difficulty.{" "}
              <B>The bank line is net of everything</B>, so no tier below the
              first can match on a raw amount — every one of them has to rebuild
              the settlement from its legs first.{" "}
              <B>The refund belongs to a different cycle</B>, so a matcher that
              assumes a settlement&apos;s orders are the orders captured in its
              own period gets it wrong.{" "}
              <B>The fee base is the settlement&apos;s own payment legs and
              nothing else</B> — derive it from the net, or from gross minus
              refunds, and the batch does not close, because real discount fees
              are not returned when a payment is refunded. And{" "}
              <B>the chargeback names an order that does not exist</B> in the
              register: it is dropped from the settlement&apos;s order set and
              written to the audit trail, but it is not raised as an exception,
              because the arithmetic still closes.
            </P>
            <P>
              Money is an integer number of paise everywhere — on the wire, in
              storage, and in this interface. Never a float: 2.36% of ₹49,320.00
              in binary floating point is not a whole number of paise, and the
              error compounds once per settlement. Percentages are basis points
              floored with integer division.
            </P>
          </Section>

          <Section id="pipeline" title="The pipeline">
            <P>
              Seven stages, each with an explicit list of what it may not do.
              The bans are the interesting half: a stage that may derive a view
              but may not decide a match cannot quietly become a matcher.
            </P>
            <ol className="mt-6 space-y-5">
              {PIPELINE.map((stage, i) => (
                <li key={stage.name} className="flex gap-4">
                  <span className="tnum mt-0.5 w-5 shrink-0 font-mono text-xs text-muted-foreground">
                    {String(i + 1).padStart(2, "0")}
                  </span>
                  <div className="min-w-0 space-y-1.5">
                    <p className="text-sm font-medium">
                      {stage.name}{" "}
                      <span className="font-mono text-xs font-normal text-muted-foreground">
                        {stage.module}
                      </span>
                    </p>
                    <p className="text-sm leading-relaxed text-muted-foreground">
                      {stage.does}
                    </p>
                    <p className="text-xs leading-relaxed text-muted-foreground">
                      <span className="font-medium text-foreground">
                        May not:
                      </span>{" "}
                      {stage.mayNot}
                    </p>
                  </div>
                </li>
              ))}
            </ol>
            <P>
              Stage six is the load-bearing idea: the model proposes and
              deterministic code disposes. The headline number stays checkable
              even though a non-deterministic component participated, because
              nothing non-deterministic can put a number into the output.
            </P>
          </Section>

          <Section id="ladder" title="The tier ladder">
            <P>
              Four deterministic tiers, run in order. A subject matched at an
              earlier tier leaves the pool before the next one runs.
            </P>
            <Scroller>
              <table className="w-full min-w-[38rem] border-collapse text-left">
                <thead>
                  <tr className="border-b border-border">
                    <Th className="w-14">Tier</Th>
                    <Th>Rule</Th>
                    <Th className="w-36">Payment legs</Th>
                    <Th className="w-24 text-right">Tolerance</Th>
                    <Th className="w-24 text-right">Window</Th>
                  </tr>
                </thead>
                <tbody>
                  {TIERS.map((tier) => (
                    <tr key={tier.tier} className="border-b border-border/60">
                      <td className="py-2.5 pr-4 font-mono text-xs font-medium">
                        {tier.tier}
                      </td>
                      <td className="py-2.5 pr-4 text-xs">{tier.rule}</td>
                      <td className="py-2.5 pr-4 text-xs text-muted-foreground">
                        {tier.cardinality}
                      </td>
                      <td className="tnum py-2.5 pr-4 text-right text-xs">
                        {tier.tolerance}
                      </td>
                      <td className="tnum py-2.5 text-right text-xs">
                        {tier.window}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </Scroller>

            <H3>The first tier requires the arithmetic as well as the reference</H3>
            <P>
              A settlement id in a narration proves <B>identity</B>. It says
              nothing about whether the sum closes. So T0 finds the reference,
              reconstructs the settlement, and returns no candidate at all if the
              delta is non-zero — writing the reason into the audit trail and
              letting the line fall through. Without that clause T0 would claim a
              line at confidence 1.00 and write a match whose net is not the bank
              credit, and it would swallow the one defect whose entire purpose is
              to exercise the tolerance boundary, so the tolerant tier would
              never run end to end.
            </P>

            <H3>T1 and T2 split by cardinality, not by method</H3>
            <P>
              Every tier below T0 reconstructs from legs, because the bank credit
              is net of fees. Method therefore cannot distinguish the two — an
              earlier draft defined T1 as &ldquo;reconstruction plus date
              window&rdquo;, which is T2&apos;s method exactly, so T1 subsumed T2
              and the tier the project exists for would never have fired. The
              distinction is the number of <B>payment</B> legs: one, or more than
              one. Fee, tax, refund, chargeback, reserve and adjustment legs do
              not count — a settlement with one payment, one fee and one tax leg
              settles one order, and the deduction legs are the arithmetic rather
              than the batch.
            </P>

            <H3>Why candidacy must be blind to cardinality</H3>
            <P>
              This is a real bug that was caught in review, and it is the most
              interesting thing in the matcher. The tempting implementation is
              that T1 searches the pool of one-payment-leg settlements and T2
              searches the pool of many-payment-leg ones. Every local rule then
              reads as correct. T1&apos;s rule is right, T2&apos;s rule is right,
              the ambiguity rule is right, and the tests for each pass.
            </P>
            <Figure caption="Two settlements with an identical net on an identical date. Partition the pool first and each tier sees exactly one candidate, calls it unique, and guesses.">
              {`                CORRECT                     BROKEN
                (cardinality-blind)         (pool partitioned first)

  T1 sees   candidates {K9, M2}         candidates {M2}
            → 2 candidates, ambiguous   → 1 candidate, "unique"
            → match nothing             → MATCH M2   (a guess)

  T2 sees   candidates {K9, M2}         candidates {K9}
            → 2 candidates, ambiguous   → 1 candidate, "unique"
            → match nothing             → MATCH K9   (a guess)`}
            </Figure>
            <P>
              The cardinality filter has become a tie-breaker on an ambiguous
              set, which is the one thing the design forbids outright — and it
              did so implicitly, as a side effect of an optimisation, in a place
              where no rule says &ldquo;tie-break&rdquo;. That one settlement
              happens to batch two orders and the other does not is not a
              distinguishing signal; it is an accident of shape. So the rule is
              that a subject&apos;s candidate set is <B>every</B> unclaimed
              settlement satisfying the arithmetic and the date window at any
              payment-leg count, the ambiguity rule applies to that whole set,
              and only once a single candidate survives does its leg count decide
              whether the match is labelled T1 or T2.
            </P>
          </Section>

          <Section id="ambiguity" title="The ambiguity rule">
            <Pull>More than one valid candidate means match nothing.</Pull>
            <P>
              The mirror image is enforced too: when one settlement is the only
              candidate of two different subjects, matching either would make
              iteration order the tie-breaker, so neither is matched. The same
              rule appears a third time in order recovery, which computes every
              leg&apos;s candidate set against an unmutated pool in one pass and
              accepts only uncontested singletons in a second — because a single
              pass that claims as it goes lets the row read first win, which is
              statement order as tie-breaker.
            </P>
            <P>
              The reason is an accounting reason, not a software one.{" "}
              <B>A false match is worse than no match.</B> An unmatched bank line
              is an exception on a list a human works through; a wrongly matched
              one is two wrong ledger entries nobody is looking for. Guessing
              under ambiguity is precisely how false matches are created, so the
              system declines and says so. That is why the false-match rate and
              the trap-capture rate are reported beside the headline match rate
              rather than below the fold: a match rate on its own is not a
              result.
            </P>
          </Section>

          <Section id="verifier" title="The verifier">
            <P>
              When the analyst layer runs, the model proposes a hypothesis and
              deterministic code decides. The verifier is pure Python with no
              matcher state, no engine and no pool. It runs six checks in a fixed
              order, all of which must hold, and it returns a verdict for every
              input including a malformed one — a crash in the verifier would
              take a whole reconciliation run down over one bad model response.
            </P>
            <Figure caption="Six branches over five frozen label spellings. verify() returns on the first failure.">
              {`existence → exclusivity → causality → arithmetic → coherence → uniqueness`}
            </Figure>
            <dl className="mt-7 space-y-7">
              {VERIFIER_CHECKS_DETAIL.map((check) => (
                <div key={check.name} className="space-y-2">
                  <dt className="font-mono text-sm font-medium">{check.name}</dt>
                  <dd className="space-y-2">
                    <p className="text-sm leading-relaxed">
                      <span className="font-medium">Rejects:</span>{" "}
                      {check.rejects}
                    </p>
                    <p className="text-sm leading-relaxed text-muted-foreground">
                      <span className="font-medium text-foreground">
                        Why it exists:
                      </span>{" "}
                      {check.why}
                    </p>
                  </dd>
                </div>
              ))}
            </dl>
            <P>
              Two of those deserve their own paragraph, because each exists
              because of a demonstrated exploit rather than in anticipation of
              one.
            </P>
            <H3>Why coherence exists</H3>
            <P>
              The first four checks each test the proposed set <B>in isolation</B>
              . Uniqueness enumerates whole settlements, but it never asks
              whether the proposed set <B>is</B> one of them. So a leg set
              cherry-picked across settlement boundaries — one that happens to
              sum to the bank credit — sails through with zero or one closer. Two
              mutually exclusive hypotheses were accepted for one bank line
              before this check existed. Unconstrained subset-sum over the leg
              pool is not reconciliation, so a proposal must <B>be</B> a
              settlement: one settlement id, all of its legs.
            </P>
            <P>
              And the fix for that hole contained another. The expected leg set
              is derived from the map of transactions by id, not from the map of
              transactions by settlement, because that second map answers only
              &ldquo;was this settlement ingested&rdquo; and is not the authority
              on what it contains. A caller that built it from <B>unclaimed</B>{" "}
              legs would present a partly-claimed settlement as a complete one,
              and the remainder would be laundered into a whole settlement that
              closes the gross — the cherry-picked leg set returning through the
              check written to stop it. An empty map now fails closed and loudly;
              a partial one used to fail open and silently.
            </P>
            <H3>Why uniqueness exists, and why it is separate</H3>
            <P>
              On an ambiguous pair the two competing candidate sets are{" "}
              <B>disjoint</B> and neither has been claimed, so exclusivity never
              fires. Every id exists. Both close arithmetically. Both settled on
              or before the bank date. All four earlier checks pass on both
              hypotheses and both get accepted — the trap-capture rate silently
              goes to zero and the false-match rate rises, but only when the
              analyst is switched on. It is the same ambiguity rule the
              deterministic tiers obey:{" "}
              <B>
                a model must not be permitted to resolve what deterministic code
                correctly refused.
              </B>{" "}
              It runs last because it is the only check not about the proposed
              set at all, and there is no point enumerating alternatives to a
              hypothesis that already failed on its own terms.
            </P>
            <P>
              Coherence reports under the <Code>existence</Code> label rather
              than under a sixth spelling of its own, because the five check
              names are a frozen contract shared with the exception record. Six
              branches, five labels, and the tuple says so in a comment.
              Existence is the honest one of the five: the settlement the
              hypothesis implicitly names does not exist as proposed.
            </P>
            <P>
              A rejected hypothesis is a feature, not a failure. It is the
              visible evidence that the guardrail fires, so the proposal text,
              the free-text reason and the machine-readable failing check all
              survive onto the exception this interface renders, and the
              rejection rate reports the count explicitly.
            </P>
          </Section>

          <Section id="defects" title="The ten defect classes">
            <P>
              The dataset is seeded and adversarial, and every defect is labelled
              in the ground truth. One of the ten is a trap set for this
              system&apos;s own benefit: if the matcher resolves it, the
              trap-capture rate drops below 1.0 and the failure is visible in the
              headline.
            </P>
            <Scroller>
              <table className="w-full min-w-[40rem] border-collapse text-left">
                <thead>
                  <tr className="border-b border-border">
                    <Th className="w-48">Defect</Th>
                    <Th>What it tests</Th>
                    <Th className="w-40">Resolved by</Th>
                  </tr>
                </thead>
                <tbody>
                  {DEFECT_CLASSES.map((defect) => (
                    <tr key={defect.name} className="border-b border-border/60">
                      <td className="py-2.5 pr-4 font-mono text-xs">
                        {defect.name}
                      </td>
                      <td className="py-2.5 pr-4 text-xs text-muted-foreground">
                        {defect.tests}
                      </td>
                      <td className="py-2.5 text-xs">
                        <span
                          className={
                            defect.unresolved
                              ? "text-excepted-fg"
                              : "text-foreground"
                          }
                        >
                          {defect.resolvedBy}
                        </span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </Scroller>
            <P>
              The distribution across tiers is lopsided by construction, and it
              decomposes exactly. T0 takes every line whose narration or bank
              reference names its settlement <B>and</B> whose arithmetic closes
              to the paise — which is every clean settlement plus the six defects
              that damage a settlement&apos;s content while leaving its reference
              intact and its sum exact. T1 and T2 together are exactly the
              garbled-narration lines, because that is the only defect that
              removes the reference from a line that is still resolvable, and the
              split between them is payment-leg cardinality and nothing else. The
              tolerant tier is exactly the rounding-break lines.
            </P>
          </Section>

          <Section id="limitation" title="The known limitation: split settlements">
            <P>
              One settlement paid across two bank lines. It breaks the identity
              every other defect leans on: every other settlement satisfies{" "}
              <Code>Σ legs = its bank line&apos;s credit</Code>, and this one
              satisfies <Code>Σ legs = credit₁ + credit₂</Code> and closes
              neither line on its own.
            </P>
            <Figure caption="Both narrations name the settlement, so the reference lookup hits — and then declines, because a settlement id proves identity and never arithmetic.">
              {`BL-0065  "... SETL setl_00064 PART 1 OF 2"  credit   86,75,241 paise
BL-0168  "... SETL setl_00064 PART 2 OF 2"  credit   86,75,242 paise
                                setl_00064 net     1,73,50,483 paise

[T0:reference-hit-arithmetic-declined] reference setl_00064 hit, but it
reconstructs to 17350483 paise against credit 8675241 — residual
delta=8675242 paise (net − credit).`}
            </Figure>
            <P>
              The engine is not silently wrong here. It names the right
              settlement, reports the exact residual, and refuses to guess — and
              note what the residual is: precisely the other half&apos;s credit.
              The exception evidence contains the answer. What the engine lacks
              is not information but a tier that can form the hypothesis
              &ldquo;these two bank lines are one settlement&rdquo;.
            </P>
            <P>
              It was left unimplemented on purpose, and saying so plainly is
              worth more than a higher headline number. It is a new tier, not a
              repair: matching it means searching for a subset of bank lines
              whose credits sum to one settlement&apos;s net, and a
              sum-of-subsets search over an ambiguous candidate set is exactly
              where a tie-breaker creeps back in — &ldquo;the combination that
              sums closest&rdquo; is a tie-breaker, and at scale there will be
              spurious pairs. Bolting it on would put a false-match rate of
              exactly zero at risk to gain a few points on a metric worth less
              than it. The analyst path cannot express it either: a hypothesis
              carries a single bank line id, and there is nowhere to put the
              second.
            </P>
            <P>
              So it is reported as an exception with a machine-readable reason
              code, on a list a human can work through, and named here. It is the
              highest-value next piece of work in this repository, and it needs
              its own round.
            </P>
          </Section>

          <Section id="caveats" title="What the numbers do not prove">
            <P>
              Four separate times, a fully green scorecard or test suite failed
              to establish the thing it appeared to establish. Each was found by
              mutating the code — deliberately breaking the implementation and
              checking that something went red — rather than by reading it. In
              every case the metrics were, and remained, green. That is the most
              useful fact about this project, and it is why every number in this
              console carries its own caveat rather than a badge.
            </P>
            <ol className="mt-6 space-y-4">
              {LIMITATIONS.map((limit, i) => (
                <li key={limit.title} className="flex gap-4">
                  <span className="tnum mt-0.5 w-5 shrink-0 font-mono text-xs text-muted-foreground">
                    {String(i + 1).padStart(2, "0")}
                  </span>
                  <div className="min-w-0 space-y-1">
                    <p className="text-sm font-medium">{limit.title}</p>
                    <p className="text-sm leading-relaxed text-muted-foreground">
                      {limit.detail}
                    </p>
                  </div>
                </li>
              ))}
            </ol>
          </Section>
        </article>

        <MethodNav sections={SECTIONS} />
      </div>
    </div>
  );
}

/* ---------------------------------------------------------------- *
 * Prose primitives. Local to this page: they exist to hold one
 * measure and one rhythm, and nothing else in the app reads like this.
 * ---------------------------------------------------------------- */

function Section({
  id,
  title,
  children,
}: {
  id: string;
  title: string;
  children: React.ReactNode;
}) {
  return (
    <section id={id} className="scroll-mt-24 pt-16">
      <h2 className="text-lg font-medium tracking-tight">{title}</h2>
      <div className="mt-5">{children}</div>
    </section>
  );
}

function H3({ children }: { children: React.ReactNode }) {
  return (
    <h3 className="mt-10 mb-3 text-sm font-medium tracking-tight">{children}</h3>
  );
}

function P({ children }: { children: React.ReactNode }) {
  return (
    <p className="mt-4 text-base leading-relaxed first:mt-0">{children}</p>
  );
}

function B({ children }: { children: React.ReactNode }) {
  return <strong className="font-medium">{children}</strong>;
}

function Code({ children }: { children: React.ReactNode }) {
  return <code className="font-mono text-sm">{children}</code>;
}

function Pull({ children }: { children: React.ReactNode }) {
  return (
    <p className="border-l border-brand py-1 pl-5 text-base leading-relaxed font-medium">
      {children}
    </p>
  );
}

/** A worked figure. Monospace because it is aligned data, not a costume. */
function Figure({
  children,
  caption,
}: {
  children: string;
  caption: string;
}) {
  return (
    <figure className="mt-6">
      <div className="overflow-x-auto rounded-lg border border-border bg-surface px-4 py-3.5">
        <pre className="tnum font-mono text-xs leading-relaxed whitespace-pre">
          {children}
        </pre>
      </div>
      <figcaption className="mt-2.5 text-xs leading-relaxed text-muted-foreground">
        {caption}
      </figcaption>
    </figure>
  );
}

function Scroller({ children }: { children: React.ReactNode }) {
  return <div className="mt-6 overflow-x-auto">{children}</div>;
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
      className={`pb-2 text-2xs font-medium text-muted-foreground ${className ?? ""}`}
    >
      {children}
    </th>
  );
}

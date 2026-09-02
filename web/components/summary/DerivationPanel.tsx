import type { Derivation } from "@/lib/derivations";
import { SUBJECT_NOTE } from "@/lib/derivations";
import type { Metrics, RunSummary } from "@/lib/types";

/**
 * A number's own arithmetic, rendered where the number is.
 *
 * Definition, then the numerator over the denominator as an actual fraction,
 * then the values from this run that feed it, then what it does not prove.
 * The caveat is not smaller, not greyer and not collapsed — METRICS.md
 * records four separate occasions on which a green metric failed to establish
 * what it appeared to establish, so the caveat is the half of the number that
 * carries the information.
 */
export function DerivationPanel({
  derivation,
  run,
  metrics,
}: {
  derivation: Derivation;
  run: RunSummary;
  metrics: Metrics;
}) {
  const inputs = derivation.inputs(run, metrics);

  return (
    <div className="grid gap-8 lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
      <div className="space-y-6">
        <p className="max-w-[68ch] text-sm leading-relaxed">
          {derivation.definition}
        </p>

        <div>
          <Caption>Numerator over denominator</Caption>
          <div className="mt-2.5 inline-block max-w-full">
            <p className="text-xs leading-relaxed">{derivation.numerator}</p>
            <div
              aria-hidden
              className="my-1.5 h-px w-full bg-foreground/40"
            />
            <p className="text-xs leading-relaxed">{derivation.denominator}</p>
          </div>
        </div>

        <p className="max-w-[68ch] text-2xs leading-relaxed text-muted-foreground">
          {SUBJECT_NOTE}
        </p>
      </div>

      <div className="space-y-6">
        <div>
          <Caption>From this run</Caption>
          <dl className="mt-2.5 space-y-2.5">
            {inputs.map((input) => (
              <div key={input.label}>
                <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-0.5">
                  <dt className="font-mono text-2xs text-muted-foreground">
                    {input.label}
                  </dt>
                  <dd className="tnum text-xs font-medium">{input.value}</dd>
                </div>
                {input.note ? (
                  <p className="mt-0.5 max-w-[62ch] text-2xs leading-relaxed text-muted-foreground">
                    {input.note}
                  </p>
                ) : null}
              </div>
            ))}
          </dl>
        </div>

        {derivation.caveat ? (
          <div className="border-l border-border pl-4">
            <Caption>What it does not prove</Caption>
            <p className="mt-2 max-w-[62ch] text-xs leading-relaxed">
              {derivation.caveat}
            </p>
          </div>
        ) : null}
      </div>
    </div>
  );
}

/**
 * A field label for a data group. Not an eyebrow: nothing below it is a
 * heading, and it never introduces a section — it names the values beside it.
 */
function Caption({ children }: { children: React.ReactNode }) {
  return (
    <p className="text-2xs font-medium text-muted-foreground">{children}</p>
  );
}

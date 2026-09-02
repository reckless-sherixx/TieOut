"use client";

import * as React from "react";
import { useRouter } from "next/navigation";
import { api } from "@/lib/api";
import {
  SOURCE_ROLE_LABEL,
  coverageOf,
  type Upload,
} from "@/lib/uploads";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { Panel, PanelHeader } from "@/components/Panel";

const int = (n: number) => n.toLocaleString("en-IN");

/**
 * Reconcile the selected files. `POST /api/runs` with `upload_ids`.
 *
 * **The same operation the demo path uses, with a different source.** Below
 * the API handler the two are one job, so a run over a merchant's own exports
 * reaches the same matcher, the same tiers and the same audit trail as the
 * generated dataset on the Runs page. That is the claim this button is making
 * and it is worth saying on the screen.
 *
 * MISSING SOURCES ARE A CONSEQUENCE, NOT A GATE. A selection with no orders is
 * a legitimate run -- a merchant reconciling COD remittances against a bank
 * statement has no Shopify export -- so the panel says what will happen
 * ("nothing can be matched to an order") instead of disabling the button and
 * leaving them to guess why. The only thing that actually blocks a run is a
 * selection holding no records at all, which the API refuses too.
 *
 * **The run will have no scorecard, and this says so before it starts.** There
 * is no ground truth for a merchant's own files, so `Metrics` is null and the
 * summary reports what was found rather than how well. Discovering that after
 * a run finishes would read as a broken page.
 */
export function RunFromUploads({
  uploads,
  onStarted,
}: {
  /** The selected uploads, in listing order. */
  uploads: Upload[];
  onStarted: () => void;
}) {
  const router = useRouter();
  const [useLlm, setUseLlm] = React.useState(false);
  const [submitting, setSubmitting] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  const { covered, missing, records } = coverageOf(uploads);
  const runnable = records > 0;

  async function start() {
    if (!runnable || submitting) return;
    setSubmitting(true);
    setError(null);
    try {
      const run = await api.createRun({
        upload_ids: uploads.map((upload) => upload.upload_id),
        use_llm: useLlm,
      });
      onStarted();
      router.push(`/runs/${run.run_id}`);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Panel>
      <PanelHeader
        title={`Reconcile ${uploads.length} ${uploads.length === 1 ? "file" : "files"}`}
        description="The same engine the generated datasets run through — same tiers, same verifier, same audit trail. What differs is only where the records came from."
      />

      <div className="space-y-6 border-t border-border px-6 py-6">
        <div className="grid gap-x-10 gap-y-6 lg:grid-cols-2">
          <div className="space-y-3">
            <h3 className="text-xs font-medium">What the run will read</h3>
            <ul className="space-y-1.5 text-xs">
              {covered.map((role) => (
                <li key={role} className="tnum text-muted-foreground">
                  <span className="text-foreground">
                    {SOURCE_ROLE_LABEL[role]}
                  </span>{" "}
                  ·{" "}
                  {int(
                    uploads.reduce(
                      (total, upload) =>
                        total +
                        (role === "order"
                          ? upload.order_count
                          : role === "psp_txn"
                            ? upload.psp_txn_count
                            : upload.bank_line_count),
                      0,
                    ),
                  )}{" "}
                  records
                </li>
              ))}
            </ul>
            {missing.length > 0 ? (
              <p className="max-w-prose text-xs leading-relaxed text-muted-foreground">
                No{" "}
                {missing
                  .map((role) => SOURCE_ROLE_LABEL[role].toLowerCase())
                  .join(" and no ")}{" "}
                in this selection. The run is still valid and will still report
                what it finds — but{" "}
                {missing.includes("order")
                  ? "nothing can be matched back to an order, so every settled batch will be reported without the orders behind it"
                  : missing.includes("bank_line")
                    ? "no batch can be closed against a bank credit, so every settlement will come back unmatched"
                    : "there are no settlement legs to close anything against"}
                .
              </p>
            ) : null}
          </div>

          <div className="space-y-3">
            <h3 className="text-xs font-medium">What it will not report</h3>
            <p className="max-w-prose text-xs leading-relaxed text-muted-foreground">
              No scorecard. Accuracy is measured against ground truth, and
              nobody knows the right answer to a reconciliation of your own
              files — so this run reports what it matched, what it excepted and
              why, and reports no match rate at all. A rate computed against
              its own output would be a number that grades itself.
            </p>
            <p className="max-w-prose text-xs leading-relaxed text-muted-foreground">
              The <span className="font-mono">Runs</span> page shows a seed of{" "}
              <span className="tnum font-mono">−1</span> for this run, which is
              how it says there was no seed: nothing generated these records.
            </p>
          </div>
        </div>

        <div className="flex flex-wrap items-end justify-between gap-6 border-t border-border pt-6">
          <div className="flex items-start gap-3">
            <Switch
              id="uploads-use-llm"
              checked={useLlm}
              onCheckedChange={setUseLlm}
              className="mt-1"
            />
            <div className="space-y-0.5">
              <Label htmlFor="uploads-use-llm" className="text-sm">
                LLM analyst
              </Label>
              <p className="max-w-prose text-xs leading-relaxed text-muted-foreground">
                Proposes resolutions for the unmatched residue only, each
                re-checked arithmetically before it is accepted. Off by default
                here: it adds minutes to a run and needs a credential.
              </p>
            </div>
          </div>

          <Button onClick={start} disabled={!runnable || submitting}>
            {submitting ? "Starting…" : "Reconcile these files"}
          </Button>
        </div>

        {!runnable ? (
          <p className="text-xs text-muted-foreground" role="status">
            None of the selected files produced a canonical record, so there is
            nothing to reconcile. Review their quarantine first.
          </p>
        ) : null}

        {error ? (
          <p className="text-xs text-error-fg" role="alert">
            {error}
          </p>
        ) : null}
      </div>
    </Panel>
  );
}

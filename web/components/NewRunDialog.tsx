"use client";

import * as React from "react";
import { useRouter } from "next/navigation";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { api } from "@/lib/api";
import { cn } from "@/lib/utils";

const RECORD_PRESETS = [50, 500, 5000];

/**
 * Generate a dataset, then reconcile it. Two contract operations behind one
 * button: POST /api/datasets/generate, then POST /api/runs with the returned
 * dataset id. Seed, record count and LLM on/off are the only three controls
 * the spec gives this dialog — `defect_mix` is deliberately absent, so the
 * request omits it and the generator's own default mix applies.
 */
export function NewRunDialog({ onCreated }: { onCreated?: () => void }) {
  const router = useRouter();
  const [open, setOpen] = React.useState(false);
  const [seed, setSeed] = React.useState("42");
  const [recordCount, setRecordCount] = React.useState("500");
  const [useLlm, setUseLlm] = React.useState(true);
  const [submitting, setSubmitting] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  const seedValue = Number(seed);
  const countValue = Number(recordCount);
  const valid =
    Number.isInteger(seedValue) &&
    seedValue >= 0 &&
    Number.isInteger(countValue) &&
    countValue > 0;

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (!valid || submitting) return;
    setSubmitting(true);
    setError(null);
    try {
      const dataset = await api.generateDataset({
        seed: seedValue,
        record_count: countValue,
      });
      const run = await api.createRun({
        dataset_id: dataset.dataset_id,
        use_llm: useLlm,
      });
      setOpen(false);
      onCreated?.();
      router.push(`/runs/${run.run_id}`);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger render={<Button size="sm" />}>New run</DialogTrigger>
      <DialogContent className="sm:max-w-md">
        <form onSubmit={submit} className="grid gap-5">
          <DialogHeader>
            <DialogTitle>New run</DialogTitle>
            <DialogDescription>
              Generates a seeded adversarial dataset, then reconciles it. The
              same seed and record count always produce the same dataset.
            </DialogDescription>
          </DialogHeader>

          <div className="grid gap-4">
            <div className="grid gap-2">
              <Label htmlFor="seed">Seed</Label>
              <Input
                id="seed"
                inputMode="numeric"
                value={seed}
                onChange={(e) => setSeed(e.target.value)}
                className="tnum"
              />
            </div>

            <div className="grid gap-2">
              <Label htmlFor="record-count">Record count</Label>
              <Input
                id="record-count"
                inputMode="numeric"
                value={recordCount}
                onChange={(e) => setRecordCount(e.target.value)}
                className="tnum"
              />
              <div className="flex gap-1.5 pt-0.5">
                {RECORD_PRESETS.map((preset) => (
                  <button
                    key={preset}
                    type="button"
                    onClick={() => setRecordCount(String(preset))}
                    className={cn(
                      "tnum rounded-md border border-border px-2 py-0.5 text-2xs transition-colors hover:bg-muted",
                      countValue === preset &&
                        "border-brand/50 bg-brand/10 text-brand",
                    )}
                  >
                    {preset.toLocaleString("en-IN")}
                  </button>
                ))}
              </div>
            </div>

            <div className="flex items-start justify-between gap-6 rounded-lg border border-border px-3 py-2.5">
              <div className="space-y-0.5">
                <Label htmlFor="use-llm" className="text-sm">
                  LLM analyst
                </Label>
                <p className="text-xs leading-relaxed text-muted-foreground">
                  Proposes resolutions for the unmatched residue only. Every
                  proposal is re-checked arithmetically before it is accepted.
                </p>
              </div>
              <Switch
                id="use-llm"
                checked={useLlm}
                onCheckedChange={setUseLlm}
                className="mt-1"
              />
            </div>
          </div>

          {error ? (
            <p className="text-xs text-destructive" role="alert">
              {error}
            </p>
          ) : null}

          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              onClick={() => setOpen(false)}
            >
              Cancel
            </Button>
            <Button type="submit" disabled={!valid || submitting}>
              {submitting ? "Starting…" : "Generate and reconcile"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

import { Panel, PanelHeader } from "@/components/Panel";
import { NewRunDialog } from "@/components/NewRunDialog";
import { RunTable } from "@/components/RunTable";

export default function Home() {
  return (
    <div className="mx-auto w-full max-w-[92rem] space-y-8 px-6 py-12 lg:px-8">
      <div className="flex flex-wrap items-end justify-between gap-6">
        <div className="space-y-2">
          <h1 className="text-xl font-medium tracking-tight">Runs</h1>
          <p className="max-w-[72ch] text-sm leading-relaxed text-muted-foreground">
            Each run reconciles one seeded dataset across the sales register,
            the PSP settlement report and the bank statement, and reports both
            what it matched and what it got wrong.
          </p>
        </div>
        <NewRunDialog />
      </div>

      <Panel className="overflow-hidden">
        <PanelHeader
          title="History"
          description="Auto-match and false match are shown side by side, because a wrong match is worse than no match. A run still executing has no metrics yet; those cells stay empty rather than reading 0%."
        />
        <div className="border-t border-border">
          <RunTable />
        </div>
      </Panel>
    </div>
  );
}

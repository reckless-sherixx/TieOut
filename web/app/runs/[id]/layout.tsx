import { RunScope } from "@/components/shell/RunScope";

/**
 * The nested layout every view of a run sits inside.
 *
 * It fetches the run once and holds the identity header and the view
 * navigation, so switching from the summary to the exception list does not
 * re-render the header or re-request the summary — and, more importantly, so
 * the six views cannot disagree about what state the run is in.
 */
export default async function RunLayout(props: LayoutProps<"/runs/[id]">) {
  const { id } = await props.params;
  return <RunScope runId={id}>{props.children}</RunScope>;
}

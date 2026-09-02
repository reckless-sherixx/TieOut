"use client";

import * as React from "react";
import {
  ChevronLeftIcon,
  ChevronRightIcon,
  ChevronsLeftIcon,
  ChevronsRightIcon,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/States";

/**
 * Server-side paging, written once for both explorer listings.
 *
 * Fifty rows per request, and the browser never holds more than one page.
 * That is the difference between a listing that is fine at fifty rows and one
 * that is still fine at five thousand, and it is invisible at the first size —
 * which is exactly why it has to be a rule rather than a judgement call.
 */
export const PAGE_SIZE = 50;

const int = (n: number) => n.toLocaleString("en-IN");

/**
 * Hold the last page that actually arrived while the next one is in flight.
 *
 * A table that empties itself between clicks moves everything below it twice
 * per page and leaves the reader nothing to read in between. The rows recede
 * and the controls disable instead.
 *
 * `group` is the identity of the ROW SET, not of the page: changing the
 * records source or the run is a different set of rows, and holding the old
 * ones across that would leave the wrong table on screen for a beat. Adjusted
 * during render rather than in an effect, because the value is derived from
 * props — an effect would paint the wrong thing once and then correct it.
 */
export function useHeldPage<T>(
  group: string,
  data: T | null,
  loading: boolean,
): { shown: T | null; stale: boolean } {
  const [held, setHeld] = React.useState<{ group: string; page: T } | null>(
    null,
  );

  if (data) {
    if (held?.page !== data) setHeld({ group, page: data });
  } else if (held !== null && held.group !== group) {
    setHeld(null);
  }

  const heldPage = held?.group === group ? held.page : null;

  return {
    shown: data ?? heldPage,
    stale: loading && data === null && heldPage !== null,
  };
}

export type PageRange = {
  page: number;
  pageCount: number;
  from: number;
  to: number;
  total: number;
};

/** The range arithmetic, so two listings cannot disagree about it. */
export function pageRange(page: number, total: number): PageRange {
  const pageCount = Math.max(1, Math.ceil(total / PAGE_SIZE));
  return {
    page,
    pageCount,
    from: total === 0 ? 0 : (page - 1) * PAGE_SIZE + 1,
    to: Math.min(page * PAGE_SIZE, total),
    total,
  };
}

/**
 * The row count and the four page controls.
 *
 * `unit` completes the sentence "1–50 of 166 …", so it is a plural noun and
 * never a label: "settlements", "bank lines".
 */
export function PageBar({
  range,
  unit,
  busy,
  onPageChange,
}: {
  range: PageRange;
  unit: React.ReactNode;
  busy: boolean;
  onPageChange: (next: number) => void;
}) {
  const { page, pageCount, from, to, total } = range;

  return (
    <div className="flex flex-wrap items-center justify-between gap-4">
      <p className="tnum text-xs text-muted-foreground" role="status">
        {int(from)}–{int(to)} of {int(total)} {unit}, {PAGE_SIZE} per request
      </p>

      <div className="flex items-center gap-1">
        <PageButton
          label="First page"
          icon={<ChevronsLeftIcon />}
          disabled={page <= 1 || busy}
          onClick={() => onPageChange(1)}
        />
        <PageButton
          label="Previous page"
          icon={<ChevronLeftIcon />}
          disabled={page <= 1 || busy}
          onClick={() => onPageChange(Math.max(1, page - 1))}
        />
        <span className="tnum px-2 text-xs text-muted-foreground">
          Page {int(page)} of {int(pageCount)}
        </span>
        <PageButton
          label="Next page"
          icon={<ChevronRightIcon />}
          disabled={page >= pageCount || busy}
          onClick={() => onPageChange(Math.min(pageCount, page + 1))}
        />
        <PageButton
          label="Last page"
          icon={<ChevronsRightIcon />}
          disabled={page >= pageCount || busy}
          onClick={() => onPageChange(pageCount)}
        />
      </div>
    </div>
  );
}

function PageButton({
  label,
  icon,
  disabled,
  onClick,
}: {
  label: string;
  icon: React.ReactNode;
  disabled: boolean;
  onClick: () => void;
}) {
  return (
    <Button
      variant="outline"
      size="icon-sm"
      aria-label={label}
      disabled={disabled}
      onClick={onClick}
    >
      {icon}
    </Button>
  );
}

/**
 * PAGED PAST THE END IS NOT AN EMPTY LIST.
 *
 * `?page=99` on a listing with 40 rows returns zero items and a `total` of 40.
 * A branch that reads only `items.length` turns that into "this run has none of
 * these", which is a false statement about the engine's output produced by a
 * stale bookmark -- and on the exception list it was rendered directly beneath
 * a census panel reporting all forty on the same screen.
 *
 * `total` is the fact that separates the two and it is on every response. The
 * page number is deliberately NOT clamped: a URL that was silently corrected
 * and the URL that was followed would then mean different things, which is the
 * same rule the records view applies to an out-of-enum source.
 */
export function PastTheEnd({
  range,
  unit,
  onPageChange,
}: {
  range: PageRange;
  /** Completes "there are 166 …". A plural noun. */
  unit: React.ReactNode;
  onPageChange: (next: number) => void;
}) {
  const { page, pageCount, total } = range;
  return (
    <EmptyState
      title={`Page ${int(page)} is past the end of this list`}
      reason={
        <>
          There {total === 1 ? "is" : "are"} <span className="tnum">{int(total)}</span>{" "}
          {unit} in this run, which is <span className="tnum">{int(pageCount)}</span>{" "}
          {pageCount === 1 ? "page" : "pages"} at {PAGE_SIZE} per request. The URL
          asks for one that does not exist. Nothing is missing and nothing is
          broken — this is the end of the list, not an empty one, and the page
          number was not silently corrected because a corrected URL and the URL
          you followed would then mean different things.
        </>
      }
      action={
        <Button
          variant="outline"
          size="sm"
          onClick={() => onPageChange(pageCount)}
        >
          Go to page {int(pageCount)}
        </Button>
      }
    />
  );
}

/** True when this page number is beyond a non-empty list. */
export function isPastTheEnd(range: PageRange, rowCount: number): boolean {
  return rowCount === 0 && range.total > 0 && range.page > range.pageCount;
}

/** A column header, at the one size and weight both listings use. */
export function Th({
  children,
  className,
  scope = "col",
}: {
  children: React.ReactNode;
  className?: string;
  scope?: "col" | "row";
}) {
  return (
    <th
      scope={scope}
      className={cn(
        "py-2.5 pr-4 text-2xs font-medium text-muted-foreground",
        className,
      )}
    >
      {children}
    </th>
  );
}

/**
 * The first load: a skeleton, because the shape of what is coming is known —
 * fifty rows of N columns. A spinner would say only that something is
 * happening.
 */
export function TableSkeleton({
  label,
  columns,
}: {
  label: string;
  columns: readonly string[];
}) {
  return (
    <div
      aria-busy="true"
      className="relative overflow-hidden rounded-xl border border-border"
    >
      <span className="sr-only" role="status">
        {label}
      </span>
      <div className="border-b border-border bg-surface px-4 py-2.5">
        <span aria-hidden className="skeleton block h-2.5 w-24" />
      </div>
      {Array.from({ length: 8 }, (_, row) => (
        <div
          key={row}
          className="flex items-center gap-4 border-b border-border px-4 py-3 last:border-b-0"
        >
          {columns.map((width, col) => (
            <span
              key={col}
              aria-hidden
              className="skeleton block h-3"
              style={{ width }}
            />
          ))}
        </div>
      ))}
    </div>
  );
}

/**
 * A legitimately absent field. Nullability in this contract is load-bearing —
 * a PSP leg with no `order_id` IS the missing-order-reference defect — so a
 * null is written as a null and told apart from a blank cell.
 */
export function Absent({ note }: { note: string }) {
  return (
    <span className="text-muted-foreground">
      null<span className="sr-only"> — {note}</span>
    </span>
  );
}

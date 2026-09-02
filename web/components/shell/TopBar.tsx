"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { cn } from "@/lib/utils";
import { ThemeToggle } from "@/components/ThemeToggle";

/**
 * The persistent top bar.
 *
 * Two destinations and a theme toggle. It carries product identity and
 * nothing else: there is no search, no breadcrumb and no page title here,
 * because in Operate mode the bar is the thing you stop looking at after the
 * first minute, and everything that changes per screen belongs to the screen.
 *
 * It sits on --surface rather than on a translucent page ground. A blurred
 * bar is decoration; an opaque one is a layer.
 */
const DESTINATIONS = [
  { href: "/", label: "Runs", match: (p: string) => p === "/" || p.startsWith("/runs") },
  {
    href: "/uploads",
    label: "Files",
    match: (p: string) => p.startsWith("/uploads"),
  },
  { href: "/method", label: "Method", match: (p: string) => p.startsWith("/method") },
];

export function TopBar() {
  const pathname = usePathname();

  return (
    <header className="sticky top-0 z-40 border-b border-border bg-surface text-surface-foreground">
      {/* THE GAP SHRINKS BEFORE THE BAR DOES. A third destination pushed the
          theme toggle 12 px past the right edge at 375 and gave the whole
          console a horizontally scrolling page -- the one layout rule this
          project holds every screen to. The row is a flex line with three
          items and no wrapping, so the only honest place to take the space
          from is the gutter between them. */}
      <div className="mx-auto flex h-13 w-full max-w-[92rem] items-center gap-4 px-4 sm:gap-8 sm:px-6 lg:px-8">
        <Link
          href="/"
          className="flex shrink-0 items-center gap-2.5 rounded-sm text-sm font-medium tracking-tight focus-visible:focus-ring"
        >
          <Mark />
          Tieout
        </Link>

        <nav aria-label="Primary" className="flex min-w-0 items-center gap-0.5 sm:gap-1">
          {DESTINATIONS.map((d) => {
            const current = d.match(pathname);
            return (
              <Link
                key={d.href}
                href={d.href}
                aria-current={current ? "page" : undefined}
                className={cn(
                  "rounded-md px-2 py-1 text-xs whitespace-nowrap transition-colors duration-150 sm:px-2.5",
                  "focus-visible:focus-ring",
                  current
                    ? "bg-surface-selected text-foreground"
                    : "text-muted-foreground hover:bg-surface-hover hover:text-foreground active:bg-surface-active",
                )}
              >
                {d.label}
              </Link>
            );
          })}
        </nav>

        <div className="ml-auto shrink-0">
          <ThemeToggle />
        </div>
      </div>
    </header>
  );
}

/**
 * Three sources collapsing into one credit — the shape of the whole problem,
 * at 16px. Authored SVG rather than a glyph.
 *
 * Stroke is 1.35 on a 16 viewBox, which is the same optical weight as the
 * icon library's 2 on a 24 viewBox, so the mark and the icons beside it are
 * one family. Straight segments only: at this size a curve becomes mush.
 */
function Mark() {
  return (
    <svg
      aria-hidden
      viewBox="0 0 16 16"
      className="size-4 shrink-0"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.35"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d="M1.6 4h4M1.6 8h4M1.6 12h4" className="text-muted-foreground" />
      <path d="M5.6 4 8.8 8 5.6 12M5.6 8h3.2" />
      <path d="M8.8 8h3.3" className="text-brand" />
      <circle
        cx="13.4"
        cy="8"
        r="1.25"
        fill="currentColor"
        stroke="none"
        className="text-brand"
      />
    </svg>
  );
}

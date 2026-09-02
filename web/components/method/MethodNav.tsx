"use client";

import * as React from "react";
import { cn } from "@/lib/utils";

/**
 * The contents of /method, and which part of it you are reading.
 *
 * Hidden below the large breakpoint rather than stacked: on a narrow screen
 * the page is already linear, and a list of eight links above the article
 * would push the article itself off the first screen.
 */
export function MethodNav({
  sections,
}: {
  sections: readonly { id: string; label: string }[];
}) {
  const [active, setActive] = React.useState<string>(sections[0]?.id ?? "");

  React.useEffect(() => {
    const targets = sections
      .map((s) => document.getElementById(s.id))
      .filter((el): el is HTMLElement => el !== null);
    if (targets.length === 0) return;

    // The band is the top third of the viewport: a heading becomes "current"
    // when it reaches the reading position, not when it first appears.
    const observer = new IntersectionObserver(
      (entries) => {
        const visible = entries
          .filter((e) => e.isIntersecting)
          .sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top);
        if (visible[0]) setActive(visible[0].target.id);
      },
      { rootMargin: "-88px 0px -66% 0px", threshold: 0 },
    );

    for (const target of targets) observer.observe(target);
    return () => observer.disconnect();
  }, [sections]);

  return (
    <nav
      aria-label="On this page"
      className="sticky top-24 hidden self-start lg:block"
    >
      <ul className="space-y-px border-l border-border">
        {sections.map((section) => {
          const current = active === section.id;
          return (
            <li key={section.id}>
              <a
                href={`#${section.id}`}
                aria-current={current ? "true" : undefined}
                className={cn(
                  "-ml-px block border-l py-1.5 pl-4 text-xs transition-colors duration-150",
                  "focus-visible:focus-ring",
                  current
                    ? "border-brand text-foreground"
                    : "border-transparent text-muted-foreground hover:border-border hover:text-foreground",
                )}
              >
                {section.label}
              </a>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}

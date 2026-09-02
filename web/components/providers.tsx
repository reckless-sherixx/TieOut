"use client";

import * as React from "react";
import { ThemeProvider } from "next-themes";
import { API_MOCKING_ENABLED } from "@/lib/api";

/**
 * Starts the MSW browser worker before anything is allowed to fetch.
 *
 * This is the mock half of the single API boundary: `lib/api.ts` always talks
 * to `NEXT_PUBLIC_API_BASE`, and in Wave 1 the service worker answers. Turning
 * it off for the live API is `NEXT_PUBLIC_API_MOCKING=disabled` and nothing
 * else -- no code changes, no proxy, no rewrite.
 */
function MockingGate({ children }: { children: React.ReactNode }) {
  const [state, setState] = React.useState<"pending" | "ready" | "failed">(
    API_MOCKING_ENABLED ? "pending" : "ready",
  );
  const [error, setError] = React.useState<string | null>(null);

  React.useEffect(() => {
    if (!API_MOCKING_ENABLED) return;
    let cancelled = false;
    void (async () => {
      try {
        const { startWorker } = await import("@/mocks/browser");
        await startWorker();
        if (!cancelled) setState("ready");
      } catch (cause) {
        if (cancelled) return;
        setError(cause instanceof Error ? cause.message : String(cause));
        setState("failed");
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  if (state === "pending") {
    return (
      <div className="flex flex-1 items-center justify-center p-16">
        <p className="text-sm text-muted-foreground">Starting mock API…</p>
      </div>
    );
  }

  if (state === "failed") {
    // Fail loudly rather than silently letting requests escape to a real API
    // that may not exist yet — a blank table would look like "no exceptions".
    // Reaching here means BOTH the service worker and the fetch fallback died.
    return (
      <div className="mx-auto max-w-xl p-16">
        <h1 className="text-lg font-medium tracking-tight">
          The mock API did not start
        </h1>
        <p className="mt-3 text-sm leading-relaxed text-muted-foreground">
          MSW could not register its service worker, so nothing would answer{" "}
          <code className="font-mono text-xs">lib/api.ts</code>. Reload the page;
          if it persists, check that{" "}
          <code className="font-mono text-xs">/mockServiceWorker.js</code> is
          served and that the browser allows service workers on this origin.
        </p>
        {error ? (
          <pre className="mt-4 overflow-x-auto rounded-lg border border-border bg-muted p-3 font-mono text-xs">
            {error}
          </pre>
        ) : null}
      </div>
    );
  }

  return <>{children}</>;
}

export function Providers({ children }: { children: React.ReactNode }) {
  return (
    <ThemeProvider
      attribute="class"
      defaultTheme="system"
      enableSystem
      disableTransitionOnChange
    >
      <MockingGate>{children}</MockingGate>
    </ThemeProvider>
  );
}

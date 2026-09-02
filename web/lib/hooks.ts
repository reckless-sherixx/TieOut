"use client";

import * as React from "react";

export type Resource<T> = {
  data: T | null;
  error: Error | null;
  loading: boolean;
  /** Re-run the fetch immediately. */
  refresh: () => void;
};

type PollOptions<T> = {
  /** Milliseconds between polls. 500 ms while a run is executing. */
  intervalMs?: number;
  /**
   * Return false to stop polling. A finished run is never polled again —
   * that is the whole rule, so it lives here rather than in each component.
   */
  shouldContinue?: (data: T) => boolean;
  enabled?: boolean;
};

type Settled<T> = { key: string; data: T | null; error: Error | null };

/**
 * Fetch a resource, optionally re-fetching on an interval until it settles.
 *
 * Polls are chained with `setTimeout` rather than `setInterval` so a slow
 * response can never stack requests, and the in-flight request is aborted when
 * the component unmounts or the key changes.
 *
 * The result carries the key it belongs to, so a key change reads as "loading"
 * without any state having to be reset from inside an effect body.
 */
export function usePoll<T>(
  key: string,
  fetcher: (signal: AbortSignal) => Promise<T>,
  options: PollOptions<T> = {},
): Resource<T> {
  const { intervalMs = 500, shouldContinue, enabled = true } = options;

  const fetcherRef = React.useRef(fetcher);
  const continueRef = React.useRef(shouldContinue);

  // Refs are synchronised in an effect, never assigned during render. The
  // polling loop below only reads them from inside its own callbacks.
  React.useEffect(() => {
    fetcherRef.current = fetcher;
    continueRef.current = shouldContinue;
  });

  const [settled, setSettled] = React.useState<Settled<T> | null>(null);
  const [nonce, setNonce] = React.useState(0);

  React.useEffect(() => {
    if (!enabled) return;

    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const controller = new AbortController();

    const tick = async () => {
      try {
        const data = await fetcherRef.current(controller.signal);
        if (cancelled) return;
        setSettled({ key, data, error: null });
        const keepGoing = continueRef.current?.(data) ?? false;
        if (keepGoing) timer = setTimeout(() => void tick(), intervalMs);
      } catch (cause) {
        if (cancelled) return;
        if (cause instanceof DOMException && cause.name === "AbortError") return;
        // Stop on error rather than hammering a failing endpoint every 500 ms.
        setSettled({
          key,
          data: null,
          error: cause instanceof Error ? cause : new Error(String(cause)),
        });
      }
    };

    void tick();

    return () => {
      cancelled = true;
      controller.abort();
      if (timer) clearTimeout(timer);
    };
  }, [key, intervalMs, enabled, nonce]);

  const current = settled?.key === key ? settled : null;
  const refresh = React.useCallback(() => setNonce((n) => n + 1), []);

  return {
    data: current?.data ?? null,
    error: current?.error ?? null,
    loading: enabled && current === null,
    refresh,
  };
}

/** A one-shot fetch: the same machinery with polling switched off. */
export function useResource<T>(
  key: string,
  fetcher: (signal: AbortSignal) => Promise<T>,
  enabled = true,
): Resource<T> {
  return usePoll(key, fetcher, { enabled, shouldContinue: () => false });
}

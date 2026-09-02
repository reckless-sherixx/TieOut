import { getResponse } from "msw";
import { setupWorker } from "msw/browser";
import { handlers } from "./handlers";

export const worker = setupWorker(...handlers);

let started: Promise<MockTransport> | null = null;

export type MockTransport = "service-worker" | "fetch-patch";

/**
 * Start the mock API exactly once per page load.
 *
 * The service worker is the primary transport — it is the one that shows real
 * requests in the network panel. Some browsers (locked-down profiles, private
 * windows, automation contexts) refuse to register one; rather than leaving the
 * app with nothing answering `lib/api.ts`, we fall back to routing `fetch`
 * through the same handler list via msw's `getResponse`. Both paths resolve the
 * identical handlers, so the mocked API is the same either way.
 *
 * React StrictMode mounts effects twice in development, and calling
 * `worker.start()` twice throws `cannot configure an already enabled network`,
 * so the promise is memoised at module scope.
 */
export function startWorker(): Promise<MockTransport> {
  if (started) return started;

  started = worker
    .start({
      onUnhandledRequest: "bypass",
      quiet: true,
      serviceWorker: { url: "/mockServiceWorker.js" },
    })
    .then((): MockTransport => "service-worker")
    .catch((): MockTransport => {
      patchFetch();
      return "fetch-patch";
    });

  return started;
}

let fetchPatched = false;

function patchFetch(): void {
  if (fetchPatched) return;
  fetchPatched = true;

  const nativeFetch = globalThis.fetch.bind(globalThis);

  globalThis.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
    const request = new Request(input as RequestInfo, init);
    const mocked = await getResponse(handlers, request.clone());
    return mocked ?? nativeFetch(input, init);
  };
}

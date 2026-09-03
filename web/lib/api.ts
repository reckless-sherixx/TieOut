/**
 * THE SINGLE API BOUNDARY.
 *
 * Every network call in this app goes through this module. No component calls
 * `fetch` directly, ever (LANE-E-web.md §5). That rule is the whole reason the
 * Wave 2 mock -> live swap is a one-line change:
 *
 *     NEXT_PUBLIC_API_MOCKING=disabled  NEXT_PUBLIC_API_BASE=http://localhost:8000
 *
 * There is no rewrite, no proxy and no `no-cors` mode here on purpose. If the
 * live API rejects a cross-origin request that is Lane D's CORS configuration
 * to fix, and hiding it behind a Next.js rewrite would make the dev setup
 * diverge from the deployed one (LANE-E-web.md §1).
 */
import type {
  BatchNetting,
  CreateRunRequest,
  DatasetGenerated,
  DriftReport,
  GenerateDatasetRequest,
  MatchDetail,
  PaginatedReconExceptions,
  ReasonCode,
  ReconExceptionAudited,
  RunCreated,
  RunStatus,
  RunSummary,
} from "./types";

export const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8000";

/** True when the MSW browser worker should intercept. Mocks are the default. */
export const API_MOCKING_ENABLED =
  (process.env.NEXT_PUBLIC_API_MOCKING ?? "enabled") !== "disabled";

export class ApiError extends Error {
  readonly status: number;
  readonly path: string;
  readonly detail: string | null;
  /**
   * The parsed error body, when there was one.
   *
   * `detail` is the prose every error on this API carries and is what most
   * callers render. But `POST /api/uploads` answers a refusal with a
   * STRUCTURED 422 — which of the two refusals it was, the confidence
   * threshold, and every candidate format with the score it gave the file —
   * and reducing that to its sentence would throw away the only numbers a
   * merchant can act on. So the whole body is kept, and the one caller that
   * needs more than a sentence narrows it (`lib/uploads.ts:refusalOf`).
   *
   * `unknown`, deliberately: nothing may read a field off it without
   * narrowing first, which is what stops a second endpoint quietly growing a
   * bespoke error shape that only one component knows about.
   */
  readonly body: unknown;

  constructor(
    status: number,
    path: string,
    detail: string | null = null,
    body: unknown = null,
  ) {
    super(detail ? `${status} ${path}: ${detail}` : `${status} ${path}`);
    this.name = "ApiError";
    this.status = status;
    this.path = path;
    this.detail = detail;
    this.body = body;
  }
}

/** Thrown when the request never reached the API at all (offline, DNS, CORS). */
export class ApiNetworkError extends Error {
  readonly path: string;

  constructor(path: string, cause: unknown) {
    super(`Could not reach the API at ${API_BASE}${path}`);
    this.name = "ApiNetworkError";
    this.path = path;
    this.cause = cause;
  }
}

export type RequestOptions = {
  signal?: AbortSignal;
};

/** One entry of FastAPI's request-validation error body. */
type ValidationDetail = { loc?: unknown[]; msg?: unknown };

/**
 * FASTAPI SPEAKS `detail` IN TWO SHAPES AND ONLY ONE OF THEM IS A STRING.
 *
 * Every hand-raised `HTTPException` in `api/routes.py` carries a prose string,
 * and that is the shape `NotFoundError` and `ConflictError` describe. But
 * request validation — which `api/openapi.yaml` declares a 422 for — returns a
 * LIST of objects instead, each with a `loc` path and a `msg`. Reading only the
 * string case meant a 422 surfaced as a bare `422 /api/…` with no explanation
 * of what was rejected, which is the least useful possible rendering of the one
 * error that always knows exactly what is wrong with the request.
 *
 * So the list is flattened to `query.source: Input should be 'order', …` —
 * the field the API named and the message it wrote, nothing invented. Anything
 * that is neither shape yields null, and `ApiError` renders the bare status,
 * which is still honest.
 */
function readDetail(detail: unknown): string | null {
  if (typeof detail === "string") return detail;
  if (!Array.isArray(detail) || detail.length === 0) return null;

  const lines = detail
    .map((entry) => {
      if (typeof entry !== "object" || entry === null) return null;
      const { loc, msg } = entry as ValidationDetail;
      if (typeof msg !== "string") return null;
      const where = Array.isArray(loc)
        ? loc.filter((p) => typeof p === "string" || typeof p === "number").join(".")
        : "";
      return where ? `${where}: ${msg}` : msg;
    })
    .filter((line): line is string => line !== null);

  return lines.length > 0 ? lines.join("; ") : null;
}

async function request<T>(
  path: string,
  init: RequestInit,
  options: RequestOptions = {},
): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`${API_BASE}${path}`, {
      cache: "no-store",
      signal: options.signal,
      ...init,
    });
  } catch (cause) {
    if (cause instanceof DOMException && cause.name === "AbortError") throw cause;
    throw new ApiNetworkError(path, cause);
  }

  if (!res.ok) {
    let detail: string | null = null;
    let body: unknown = null;
    try {
      body = await res.json();
      detail = readDetail((body as { detail?: unknown } | null)?.detail);
    } catch {
      /* a non-JSON error body is still an ApiError, just without a detail */
    }
    throw new ApiError(res.status, path, detail, body);
  }

  return (await res.json()) as T;
}

export function getJSON<T>(path: string, options?: RequestOptions): Promise<T> {
  return request<T>(path, { method: "GET" }, options);
}

export function postJSON<T>(
  path: string,
  body: unknown,
  options?: RequestOptions,
): Promise<T> {
  return request<T>(
    path,
    {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    },
    options,
  );
}

/**
 * A DELETE.
 *
 * Its own function rather than a `method` option on `postJSON`, because a
 * parameter that can turn a POST into a DELETE makes every call site a place
 * where a typo removes data. The verb is in the name.
 */
export function deleteJSON<T>(path: string, options?: RequestOptions): Promise<T> {
  return request<T>(path, { method: "DELETE" }, options);
}

/**
 * A multipart POST — the one request in this app that is not JSON.
 *
 * `Content-Type` is deliberately NOT set. The browser has to write it itself
 * because only the browser knows the boundary token it generated; a
 * hand-written `multipart/form-data` header produces a body the server cannot
 * split, which surfaces as an empty file rather than as an error.
 */
export function postForm<T>(
  path: string,
  form: FormData,
  options?: RequestOptions,
): Promise<T> {
  return request<T>(path, { method: "POST", body: form }, options);
}

function query(params: Record<string, string | number | undefined | null>) {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === "") continue;
    search.set(key, String(value));
  }
  const s = search.toString();
  return s ? `?${s}` : "";
}

/**
 * Ten of the thirteen operations of api/openapi.yaml, one function each. The
 * records and settlements listings live in `lib/explorer.ts` and go through the
 * same `getJSON`; `listRunMatches` has no consumer.
 *
 * Typed off the generated `lib/api-types.ts` -- if the contract changes,
 * regenerating the types breaks this file, which is exactly what should happen.
 * It did: the contract gained the three `itc_*_paise` fields and the drift
 * schemas while this file was three operations behind, and nothing failed until
 * someone ran the generator. Regenerate on every rebase that touches the
 * contract, not when something looks wrong.
 */
export const api = {
  /** POST /api/datasets/generate */
  generateDataset(body: GenerateDatasetRequest, options?: RequestOptions) {
    return postJSON<DatasetGenerated>("/api/datasets/generate", body, options);
  },

  /** POST /api/runs */
  createRun(body: CreateRunRequest, options?: RequestOptions) {
    return postJSON<RunCreated>("/api/runs", body, options);
  },

  /** GET /api/runs */
  listRuns(options?: RequestOptions) {
    return getJSON<RunSummary[]>("/api/runs", options);
  },

  /** GET /api/runs/{id} */
  getRun(id: string, options?: RequestOptions) {
    return getJSON<RunSummary>(`/api/runs/${encodeURIComponent(id)}`, options);
  },

  /** GET /api/runs/{id}/status */
  getRunStatus(id: string, options?: RequestOptions) {
    return getJSON<RunStatus>(
      `/api/runs/${encodeURIComponent(id)}/status`,
      options,
    );
  },

  /** GET /api/runs/{id}/exceptions -- server-side paginated, never client-side. */
  listRunExceptions(
    id: string,
    params: { reason_code?: ReasonCode | null; page?: number; size?: number } = {},
    options?: RequestOptions,
  ) {
    return getJSON<PaginatedReconExceptions>(
      `/api/runs/${encodeURIComponent(id)}/exceptions${query(params)}`,
      options,
    );
  },

  /** GET /api/runs/{id}/batches/{settlement_id} -- powers the netting diagram. */
  getBatchNetting(id: string, settlementId: string, options?: RequestOptions) {
    return getJSON<BatchNetting>(
      `/api/runs/${encodeURIComponent(id)}/batches/${encodeURIComponent(settlementId)}`,
      options,
    );
  },

  /**
   * GET /api/runs/{id}/drift -- what changed against a baseline run.
   *
   * `against` is OPTIONAL AND OMITTING IT IS A REAL CHOICE, not a missing
   * argument: the API's own default baseline is the immediately previous
   * COMPLETED run on the same dataset, which is the comparison a controller
   * asks for by default. `RunSummary` does not carry `dataset_id`, so the
   * client cannot compute that default itself — and must not try.
   *
   * Three answers are all normal here and all three are the caller's to render:
   * 200 with a report, 404 when there is no earlier run to compare with, and
   * 409 when both runs exist but the pair cannot be compared.
   */
  getRunDrift(id: string, against: string | null, options?: RequestOptions) {
    return getJSON<DriftReport>(
      `/api/runs/${encodeURIComponent(id)}/drift${query({ against })}`,
      options,
    );
  },

  /** GET /api/exceptions/{id} -- exception + subject + full audit trail. */
  getException(id: string, options?: RequestOptions) {
    return getJSON<ReconExceptionAudited>(
      `/api/exceptions/${encodeURIComponent(id)}`,
      options,
    );
  },

  /** GET /api/matches/{id} -- match + bank line + full audit trail. */
  getMatch(id: string, options?: RequestOptions) {
    return getJSON<MatchDetail>(
      `/api/matches/${encodeURIComponent(id)}`,
      options,
    );
  },
};

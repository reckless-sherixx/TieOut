/**
 * MSW handlers for every operation `api/openapi.yaml` declares that a console
 * route actually calls. The two with no mock are `GET /api/runs/{id}/matches`,
 * which no route calls, and the two `/api/auth/` operations, which are
 * reachable only on a deployment with `RECON_AUTH=enabled` — a configuration
 * mock mode is not.
 *
 * These mocks are the only thing standing in for Lane D. They return
 * contract-valid shapes and nothing else -- no convenience fields, no extra
 * envelopes. If the UI needs something these handlers cannot produce, that is
 * a gap in the contract to escalate, not a field to invent here.
 */
import { http, HttpResponse, delay } from "msw";
/* Artificial latency is applied to the two POSTs only, where it makes the
   "generate a dataset, then reconcile it" beat feel like real work. Every GET
   answers as fast as it can, so the exception table's responsiveness at 5,000
   rows is a real measurement and not a measurement of a sleep. */
import { API_BASE } from "@/lib/api";
import type {
  CreateRunRequest,
  DatasetGenerated,
  GenerateDatasetRequest,
  PaginatedReconExceptions,
  ReasonCode,
  RunCreated,
  RunStatus,
} from "@/lib/types";
import {
  auditedException,
  batchNetting,
  createRun,
  createUploadRun,
  driftReport,
  exceptionsFor,
  findDataset,
  findRun,
  generateDataset,
  listRuns,
  matchDetail,
  progressOf,
  stageOf,
} from "./fixtures";
import { explorerHandlers } from "./explorer";
import { uploadHandlers, uploadExists, uploadRecordCount } from "./uploads";

const url = (path: string) => `${API_BASE}${path}`;

const notFound = (detail: string) =>
  HttpResponse.json({ detail }, { status: 404 });

export const handlers = [
  /* 0. The settlements and records listings, and the match detail the
     settlement breakdown opens. First in the list because its
     GET /api/matches/{id} resolver answers only the ids the settlements
     listing hands out and returns nothing for the rest, which passes those
     straight through to handler 9 below. */
  ...explorerHandlers,

  /* 0b. The four upload operations and the store behind them. Before the run
     handlers because `POST /api/uploads` and `POST /api/runs` are different
     paths and order does not matter here -- they are grouped with the
     explorer's simply because both are self-contained sets. */
  ...uploadHandlers,

  /* 1. POST /api/datasets/generate */
  http.post(url("/api/datasets/generate"), async ({ request }) => {
    const body = (await request.json()) as GenerateDatasetRequest;
    await delay(180);
    const dataset = generateDataset(body.seed, body.record_count);
    return HttpResponse.json<DatasetGenerated>({ dataset_id: dataset.dataset_id });
  }),

  /* 2. POST /api/runs -- two sources, one run.

     `dataset_id` reconciles a generated dataset; `upload_ids` reconciles the
     records a merchant's own files produced. Exactly one of the two, and both
     or neither is the same 422 the real API answers -- a mock that accepted
     both would let the console ship a request the service refuses. */
  http.post(url("/api/runs"), async ({ request }) => {
    const body = (await request.json()) as CreateRunRequest;

    const hasDataset = body.dataset_id !== null && body.dataset_id !== undefined;
    const hasUploads = body.upload_ids !== null && body.upload_ids !== undefined;
    if (hasDataset === hasUploads) {
      return HttpResponse.json(
        {
          detail:
            "a run reconciles exactly one source: give either dataset_id or upload_ids, and not both",
        },
        { status: 422 },
      );
    }

    if (hasUploads) {
      const ids = body.upload_ids ?? [];
      const unknown = ids.find((id) => !uploadExists(id));
      if (unknown) return notFound(`no upload with id '${unknown}'`);
      if (ids.length === 0) {
        return HttpResponse.json(
          { detail: "upload_ids is empty: a run needs at least one upload to read" },
          { status: 422 },
        );
      }
      await delay(120);
      const runId = createUploadRun(body.use_llm, uploadRecordCount(ids));
      return HttpResponse.json<RunCreated>({ run_id: runId }, { status: 202 });
    }

    const dataset = findDataset(body.dataset_id ?? "");
    if (!dataset) return notFound(`No dataset ${body.dataset_id}`);
    await delay(120);
    const runId = createRun(body.use_llm, dataset.seed, dataset.record_count);
    return HttpResponse.json<RunCreated>({ run_id: runId }, { status: 202 });
  }),

  /* 3. GET /api/runs */
  http.get(url("/api/runs"), () => {
    return HttpResponse.json(listRuns());
  }),

  /* 4. GET /api/runs/{id} */
  http.get(url("/api/runs/:id"), ({ params }) => {
    const record = findRun(String(params.id));
    if (!record) return notFound(`No run ${params.id}`);
    return HttpResponse.json(record.summary);
  }),

  /* 5. GET /api/runs/{id}/status */
  http.get(url("/api/runs/:id/status"), ({ params }) => {
    const record = findRun(String(params.id));
    if (!record) return notFound(`No run ${params.id}`);
    // Deliberately undelayed: this is polled at 500 ms.
    return HttpResponse.json<RunStatus>({
      state: record.summary.state,
      progress: progressOf(record),
      stage: stageOf(record),
    });
  }),

  /* 6. GET /api/runs/{id}/exceptions */
  http.get(url("/api/runs/:id/exceptions"), ({ params, request }) => {
    const runId = String(params.id);
    const record = findRun(runId);
    if (!record) return notFound(`No run ${runId}`);

    const search = new URL(request.url).searchParams;
    const reasonCode = search.get("reason_code") as ReasonCode | null;
    const page = Math.max(1, Number(search.get("page") ?? 1));
    const size = Math.max(1, Math.min(200, Number(search.get("size") ?? 50)));

    // Stable ORDER BY exception_id: rows are built in id order and never
    // re-sorted, so no row can appear on two pages.
    const all = exceptionsFor(runId);
    const filtered = reasonCode
      ? all.filter((row) => row.reason_code === reasonCode)
      : all;

    const start = (page - 1) * size;
    return HttpResponse.json<PaginatedReconExceptions>({
      items: filtered.slice(start, start + size),
      total: filtered.length,
      page,
      size,
    });
  }),

  /* 7. GET /api/runs/{id}/batches/{settlement_id} */
  http.get(url("/api/runs/:id/batches/:settlementId"), ({ params }) => {
    const record = findRun(String(params.id));
    if (!record) return notFound(`No run ${params.id}`);
    return HttpResponse.json(
      batchNetting(String(params.id), String(params.settlementId)),
    );
  }),

  /* 8. GET /api/runs/{id}/drift
     Three answers, and all three are exercised by these fixtures: a report, a
     404 for the run that has no earlier run on its dataset, and a 409 for a
     pair of different sizes. The materiality thresholds live in
     `fixtures.ts` and are the engine's own constants -- a mock that flagged
     different moves would teach the UI a rule the real service does not have. */
  http.get(url("/api/runs/:id/drift"), ({ params, request }) => {
    const against = new URL(request.url).searchParams.get("against");
    const outcome = driftReport(String(params.id), against);
    if (outcome.kind === "not_found") return notFound(outcome.detail);
    if (outcome.kind === "conflict") {
      return HttpResponse.json({ detail: outcome.detail }, { status: 409 });
    }
    return HttpResponse.json(outcome.report);
  }),

  /* 9. GET /api/exceptions/{id} */
  http.get(url("/api/exceptions/:id"), ({ params }) => {
    const audited = auditedException(String(params.id));
    if (!audited) return notFound(`No exception ${params.id}`);
    return HttpResponse.json(audited);
  }),

  /* 10. GET /api/matches/{id} */
  http.get(url("/api/matches/:id"), ({ params }) => {
    return HttpResponse.json(matchDetail(String(params.id)));
  }),
];


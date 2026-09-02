/**
 * Deterministic, contract-valid fixtures for the MSW handlers.
 *
 * Everything here is generated from a seeded PRNG so a given run id always
 * produces the same rows -- the exception table pages over a stable
 * `ORDER BY exception_id` the way the real API guarantees, and screenshots are
 * reproducible.
 *
 * Money is integer paise everywhere, exactly as on the wire. Percentages are
 * taken in basis points with integer floor division, mirroring
 * `core/money.pct_of` -- there is not a single float amount in this file.
 */
import type {
  AuditEntry,
  BankLine,
  BatchNetting,
  BatchNettingOrderLine,
  DriftReport,
  MatchDetail,
  MetricMove,
  Metrics,
  Order,
  PSPTransaction,
  ReasonCode,
  ReasonCodeMove,
  ReconExceptionAudited,
  ReconExceptionDetail,
  RunSummary,
  SubjectRecord,
  VerifierCheck,
} from "@/lib/types";

/* ------------------------------------------------------------------ *
 * Deterministic primitives
 * ------------------------------------------------------------------ */

/** mulberry32 -- tiny, fast, and stable across runs. */
/**
 * The confidence the engine stamps per tier, measured on a live run
 * 2026-09-02: T0 1.00, T1 0.95, T2 0.99, T3 0.80, LLM 0.70.
 *
 * Hardcoded HERE and nowhere else. A mock that invented different figures
 * would let the console look correct against fiction and wrong against the
 * API -- which is the exact defect this field was added to fix, when
 * `lib/tiers.ts` carried a table saying "verified" for the engine's 0.70.
 */
const TIER_CONFIDENCE = {
  T0: { confidence_observed: 1.0, confidence_conflict: false },
  T1: { confidence_observed: 0.95, confidence_conflict: false },
  T2: { confidence_observed: 0.99, confidence_conflict: false },
  T3: { confidence_observed: 0.8, confidence_conflict: false },
  LLM: { confidence_observed: 0.7, confidence_conflict: false },
};

function prng(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function hashString(s: string): number {
  let h = 2166136261;
  for (let i = 0; i < s.length; i += 1) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return h >>> 0;
}

/** `core/money.pct_of` -- basis points, integer floor. Never a float. */
function pctOf(amountPaise: number, bps: number): number {
  return Math.floor((amountPaise * bps) / 10_000);
}

const MDR_BPS = 236;
const GST_BPS = 1800;

function pad(n: number, width: number): string {
  return String(n).padStart(width, "0");
}

function isoDate(dayOffset: number): string {
  const base = Date.UTC(2026, 7, 1); // 2026-08-01
  return new Date(base + dayOffset * 86_400_000).toISOString().slice(0, 10);
}

function isoDateTime(dayOffset: number, minuteOffset: number): string {
  const base = Date.UTC(2026, 7, 1, 9, 0, 0);
  return new Date(base + dayOffset * 86_400_000 + minuteOffset * 60_000)
    .toISOString()
    .slice(0, 19);
}

/**
 * A full RFC-3339 instant `hours` before the mock booted, for
 * `RunSummary.created_at` (`format: date-time`).
 *
 * Unlike `isoDate`/`isoDateTime` above -- which model the naive date strings
 * carried by the datasets themselves and are pinned to fixed calendar days --
 * this is anchored to the clock. `created_at` is a real instant stamped by
 * the API when the run row is created, and the mock stands in for the API
 * here. The *client* still never invents one.
 *
 * Anchored rather than hard-coded so the seeded history is coherent whenever
 * it is opened: a completed run is always in the past, and the run that is
 * still executing is always the most recent. Hard-coded calendar instants
 * drift into the future relative to `Date.now()` and produce a history where
 * finished runs look newer than the one still going.
 */
function hoursBeforeBoot(bootedAt: number, hours: number): string {
  return new Date(bootedAt - hours * 3_600_000).toISOString();
}

/* ------------------------------------------------------------------ *
 * Runs
 * ------------------------------------------------------------------ */

export type RunRecord = {
  summary: RunSummary;
  /** Wall-clock ms at which a simulated in-flight run was started, if any. */
  startedAt: number | null;
  /** How long the simulated run takes, in ms. */
  durationMs: number;
  /** Metrics revealed once the simulated run reaches `completed`. */
  pendingMetrics: Metrics | null;
  pendingMatchCount: number;
  pendingExceptionCount: number;
};

export type TierCounts = Metrics["tier_counts"];

/**
 * `tier_counts` is deliberately NOT defaulted in `metrics()` below.
 *
 * Every other metric can sensibly default to 0, but a defaulted tier
 * breakdown would let a fixture render five empty bars that nobody wrote and
 * nobody checked -- a silence dressed up as a measurement. Making it a
 * required argument means each fixture has to state what its tiers did, which
 * is the same claim the contract makes by marking all five keys required.
 */
function tiers(T0: number, T1: number, T2: number, T3: number, LLM: number): TierCounts {
  return { T0, T1, T2, T3, LLM };
}

/** Total matches produced -- the sum the run's `match_count` must equal. */
function totalMatches(t: TierCounts): number {
  return t.T0 + t.T1 + t.T2 + t.T3 + t.LLM;
}

/**
 * THE THREE ITC FIGURES ARE REQUIRED HERE FOR THE SAME REASON `tier_counts` IS.
 *
 * They are money, and a defaulted ₹0.00 would be a fixture asserting that a run
 * substantiated no input tax credit at all -- a silence dressed as a
 * measurement, and on the one register of the summary where a reader is
 * entitled to a rupee figure. Making them required means every fixture has to
 * say what its GST did, which is the claim the contract makes by marking all
 * three fields required on `Metrics`.
 *
 * `itc_variance_paise` is SIGNED and negative on real data, so the fixtures
 * carry negatives: a UI that only ever met positive values here would never
 * have its sign rendering exercised.
 */
type MetricsInput = Partial<Omit<Metrics, "tier_counts">> &
  Pick<
    Metrics,
    | "tier_counts"
    | "itc_substantiated_paise"
    | "itc_at_risk_paise"
    | "itc_variance_paise"
  >;

function metrics(m: MetricsInput): Metrics {
  return {
    auto_match_rate: 0,
    assisted_match_rate: 0,
    exception_rate: 0,
    false_match_rate: 0,
    precision: 0,
    recall_on_resolvable: 0,
    total_traps: 10,
  trap_capture_rate: 0,
    llm_rejection_rate: 0,
    throughput_records_per_sec: 0,
    llm_cost_usd_per_100: 0,
    llm_tokens_per_100: 0,
    ...m,
  };
}

/**
 * Input tax credit for a run of `recordCount` records, scaled off the
 * committed seed-42 500-record measurement in ITC-REPORT.md §3:
 * ₹39,330.31 substantiated, ₹12,066.73 at risk, −₹1,194.29 variance.
 *
 * `jitter` moves a fixture off the exact multiple so two runs of the same size
 * are not byte-identical -- which is what makes the drift view show something.
 */
function itc(
  recordCount: number,
  jitter = 1,
): Pick<
  Metrics,
  "itc_substantiated_paise" | "itc_at_risk_paise" | "itc_variance_paise"
> {
  const scale = (recordCount / 500) * jitter;
  return {
    itc_substantiated_paise: Math.round(3_933_031 * scale),
    itc_at_risk_paise: Math.round(1_206_673 * scale),
    itc_variance_paise: -Math.round(119_429 * scale),
  };
}

/**
 * The seeded run history. Deliberately mixed: a headline completed run, a
 * 5,000-exception scale run, a run still executing (metrics === null, which the
 * UI must render as a loading state and never as 0%), a small
 * deterministic-only run, and a failed run.
 *
 * MATCH COUNTS ARE TIER SUMS. `tier_counts` counts matches produced per tier
 * (spec §9), and `false_match_rate` is defined over "total matches produced",
 * so `match_count === totalMatches(tier_counts)` on every completed run here.
 * A match group bundles many records -- 500 records reconstruct to 151
 * matches, not ~500 -- so these counts are far below `record_count` and that
 * is the correct shape, not a truncation.
 */
function seedRuns(): RunRecord[] {
  const still: RunRecord["pendingMetrics"] = null;
  const bootedAt = Date.now();

  // The real 500-record breakdown. LLM=0 is a RESULT here, not a switch: the
  // analyst proposed hypotheses and the verifier refused every one, so the
  // rejection rate is 1.0 and nothing reached the assisted tier.
  const fiveHundred = tiers(136, 2, 8, 5, 0);

  // The 50-record breakdown, deterministic-only. T2 and LLM both score
  // nothing, and both must render as a visible 0.
  const fifty = tiers(11, 1, 0, 1, 0);

  const fiveThousand = tiers(1336, 20, 80, 50, 24);
  const fiftyThousand = tiers(13290, 205, 812, 502, 291);

  return [
    {
      summary: {
        run_id: "run_5f21a9",
        seed: 42,
        record_count: 5000,
        state: "completed",
        created_at: hoursBeforeBoot(bootedAt, 4.8),
        match_count: totalMatches(fiveThousand),
        exception_count: 47,
        tier_confidence: TIER_CONFIDENCE,
        metrics: metrics({
          auto_match_rate: 0.9418,
          assisted_match_rate: 0.0488,
          exception_rate: 0.0094,
          false_match_rate: 0.0,
          precision: 1.0,
          recall_on_resolvable: 0.9906,
          trap_capture_rate: 1.0,
          llm_rejection_rate: 0.439,
          throughput_records_per_sec: 1184.6,
          llm_cost_usd_per_100: 0.0213,
          llm_tokens_per_100: 1420,
          tier_counts: fiveThousand,
          ...itc(5000),
        }),
      },
      startedAt: null,
      durationMs: 0,
      pendingMetrics: still,
      pendingMatchCount: 0,
      pendingExceptionCount: 0,
    },
    {
      summary: {
        run_id: "run_c73b02",
        seed: 42,
        record_count: 50000,
        state: "completed",
        created_at: hoursBeforeBoot(bootedAt, 7.2),
        match_count: totalMatches(fiftyThousand),
        exception_count: 5000,
        tier_confidence: TIER_CONFIDENCE,
        metrics: metrics({
          auto_match_rate: 0.8712,
          assisted_match_rate: 0.0288,
          exception_rate: 0.1,
          false_match_rate: 0.0004,
          precision: 0.9996,
          recall_on_resolvable: 0.9012,
          trap_capture_rate: 1.0,
          llm_rejection_rate: 0.5121,
          throughput_records_per_sec: 1043.2,
          llm_cost_usd_per_100: 0.0219,
          llm_tokens_per_100: 1466,
          tier_counts: fiftyThousand,
          ...itc(50000, 0.97),
        }),
      },
      startedAt: null,
      durationMs: 0,
      pendingMetrics: still,
      pendingMatchCount: 0,
      pendingExceptionCount: 0,
    },
    {
      summary: {
        run_id: "run_a10e88",
        seed: 7,
        record_count: 500,
        state: "running",
        // A run that exists was created at some point, including while it is
        // still pending or running. This is never a client clock.
        created_at: new Date(bootedAt).toISOString(),
        match_count: 0,
        exception_count: 0,
        tier_confidence: TIER_CONFIDENCE,
        // Still executing: no metrics yet. Rendering 0% here would be a lie.
        metrics: null,
      },
      startedAt: bootedAt,
      durationMs: 45_000,
      pendingMetrics: metrics({
        auto_match_rate: 0.926,
        // Every hypothesis was refused, so nothing was assisted. This has to
        // agree with tier_counts.LLM === 0 or the fixture contradicts itself.
        assisted_match_rate: 0.0,
        exception_rate: 0.022,
        false_match_rate: 0.0,
        precision: 1.0,
        recall_on_resolvable: 0.978,
        trap_capture_rate: 1.0,
        llm_rejection_rate: 1.0,
        throughput_records_per_sec: 1150.0,
        llm_cost_usd_per_100: 0.0207,
        llm_tokens_per_100: 1398,
        tier_counts: fiveHundred,
        ...itc(500, 1.04),
      }),
      pendingMatchCount: totalMatches(fiveHundred),
      pendingExceptionCount: 11,
    },
    {
      summary: {
        run_id: "run_9c4d15",
        seed: 1337,
        record_count: 50,
        state: "completed",
        created_at: hoursBeforeBoot(bootedAt, 21),
        match_count: totalMatches(fifty),
        exception_count: 3,
        tier_confidence: TIER_CONFIDENCE,
        metrics: metrics({
          auto_match_rate: 0.94,
          assisted_match_rate: 0.0,
          exception_rate: 0.06,
          false_match_rate: 0.0,
          precision: 1.0,
          recall_on_resolvable: 0.94,
          trap_capture_rate: 1.0,
          // Deterministic-only: no analyst ran, so nothing was proposed and
          // nothing rejected.
          llm_rejection_rate: 0.0,
          throughput_records_per_sec: 1362.4,
          llm_cost_usd_per_100: 0.0,
          llm_tokens_per_100: 0,
          tier_counts: fifty,
          ...itc(50),
        }),
      },
      startedAt: null,
      durationMs: 0,
      pendingMetrics: still,
      pendingMatchCount: 0,
      pendingExceptionCount: 0,
    },
    {
      // A model WAS called and proposed nothing. Tokens and cost are non-zero,
      // nothing reached the assisted tier, and the rejection rate is 0.0
      // because its denominator is empty rather than because nothing was
      // refused. This is the third of the three zero-looking analyst outcomes
      // and the only one no other fixture here exercises: without it, "no model
      // ran" and "a model ran and correctly proposed nothing" would be
      // indistinguishable in the UI for the boring reason that no run produced
      // the second.
      summary: {
        run_id: "run_e402b6",
        seed: 42,
        record_count: 500,
        state: "completed",
        created_at: hoursBeforeBoot(bootedAt, 12.4),
        match_count: totalMatches(fiveHundred),
        exception_count: 20,
        tier_confidence: TIER_CONFIDENCE,
        metrics: metrics({
          auto_match_rate: 0.9379,
          assisted_match_rate: 0.0,
          exception_rate: 0.117,
          false_match_rate: 0.0,
          precision: 1.0,
          recall_on_resolvable: 0.9379,
          trap_capture_rate: 1.0,
          // Zero over zero. Nothing was proposed, so nothing was rejected.
          llm_rejection_rate: 0.0,
          throughput_records_per_sec: 1204.8,
          llm_cost_usd_per_100: 0.0084,
          llm_tokens_per_100: 612,
          tier_counts: fiveHundred,
          ...itc(500),
        }),
      },
      startedAt: null,
      durationMs: 0,
      pendingMetrics: still,
      pendingMatchCount: 0,
      pendingExceptionCount: 0,
    },
    {
      summary: {
        run_id: "run_3d94f1",
        seed: 1337,
        record_count: 50,
        state: "failed",
        created_at: hoursBeforeBoot(bootedAt, 22.6),
        match_count: 0,
        exception_count: 0,
        tier_confidence: TIER_CONFIDENCE,
        metrics: null,
      },
      startedAt: null,
      durationMs: 0,
      pendingMetrics: still,
      pendingMatchCount: 0,
      pendingExceptionCount: 0,
    },
  ];
}

const runs: RunRecord[] = seedRuns();
let runCounter = 0;

/** Advance any simulated in-flight run to where wall-clock says it should be. */
function tick(record: RunRecord): RunRecord {
  if (record.startedAt === null) return record;
  const elapsed = Date.now() - record.startedAt;

  if (elapsed < 600) {
    record.summary.state = "pending";
    return record;
  }
  if (elapsed < record.durationMs) {
    record.summary.state = "running";
    return record;
  }

  record.summary.state = "completed";
  record.summary.metrics = record.pendingMetrics;
  record.summary.match_count = record.pendingMatchCount;
  record.summary.exception_count = record.pendingExceptionCount;
  record.startedAt = null;
  return record;
}

export function progressOf(record: RunRecord): number {
  if (record.summary.state === "completed") return 1;
  if (record.summary.state === "failed") return 0;
  if (record.startedAt === null) return 0;
  const elapsed = Date.now() - record.startedAt;
  return Math.max(0, Math.min(0.995, elapsed / record.durationMs));
}

const STAGES = [
  "ingest",
  "canonicalize",
  "T0 reference match",
  "T1 single-leg match",
  "T2 batch reconstruction",
  "T3 tolerance match",
  "llm analyst",
  "verifier",
  "scoring",
];

export function stageOf(record: RunRecord): string {
  if (record.summary.state === "completed") return "done";
  if (record.summary.state === "failed") return "failed during T2 batch reconstruction";
  if (record.summary.state === "pending") return "queued";
  const p = progressOf(record);
  return STAGES[Math.min(STAGES.length - 1, Math.floor(p * STAGES.length))];
}

export function listRuns(): RunSummary[] {
  // The contract documents this response as "All runs, most recent first",
  // and `created_at` is what "recent" now means -- so the mock sorts on the
  // field rather than relying on insertion order to imply it.
  return runs
    .map((r) => tick(r).summary)
    .slice()
    .sort((a, b) => b.created_at.localeCompare(a.created_at));
}

export function findRun(id: string): RunRecord | undefined {
  const record = runs.find((r) => r.summary.run_id === id);
  return record ? tick(record) : undefined;
}

/* --- Datasets ----------------------------------------------------- */

type DatasetRecord = { dataset_id: string; seed: number; record_count: number };

const datasets = new Map<string, DatasetRecord>();
let datasetCounter = 0;

export function generateDataset(seed: number, recordCount: number): DatasetRecord {
  datasetCounter += 1;
  const record: DatasetRecord = {
    dataset_id: `ds_seed${seed}_${recordCount}_${pad(datasetCounter, 2)}`,
    seed,
    record_count: recordCount,
  };
  datasets.set(record.dataset_id, record);
  return record;
}

export function findDataset(datasetId: string): DatasetRecord | undefined {
  return datasets.get(datasetId);
}

export function createRun(useLlm: boolean, seed: number, recordCount: number): string {
  runCounter += 1;
  const runId = `run_${pad(runCounter, 2)}${hashString(`${seed}:${recordCount}:${runCounter}`)
    .toString(16)
    .slice(0, 4)}`;

  const rng = prng(hashString(runId));
  const exceptionRate = 0.012 + rng() * 0.02;
  const exceptionCount = Math.max(4, Math.round(recordCount * exceptionRate));
  const assisted = useLlm ? 0.03 + rng() * 0.03 : 0;
  const auto = 1 - assisted - exceptionCount / recordCount;

  // Matches are settlement groups, not records: roughly one match per three
  // records, following the real 500-record run's 151. The tier split is
  // lopsided by construction (spec §9) -- T0 carries almost everything and
  // the rest score in single or double digits.
  const matchCount = Math.max(5, Math.round((recordCount - exceptionCount) / 3.3));
  const llmTier = useLlm ? Math.max(1, Math.round(matchCount * 0.016)) : 0;
  const t3 = Math.max(1, Math.round(matchCount * 0.033));
  const t2 = Math.max(1, Math.round(matchCount * 0.053));
  const t1 = Math.max(1, Math.round(matchCount * 0.013));
  // T0 absorbs the remainder so the five keys sum to match_count exactly.
  const tierCounts = tiers(matchCount - llmTier - t3 - t2 - t1, t1, t2, t3, llmTier);

  runs.unshift({
    summary: {
      run_id: runId,
      seed,
      record_count: recordCount,
      state: "pending",
      // Stamped by the "API" (this mock) the moment the run row is created,
      // while it is still pending. A pending run has a created_at too.
      created_at: new Date().toISOString(),
      match_count: 0,
      exception_count: 0,
      tier_confidence: TIER_CONFIDENCE,
      metrics: null,
    },
    startedAt: Date.now(),
    durationMs: 9_000,
    pendingMetrics: metrics({
      auto_match_rate: Number(auto.toFixed(4)),
      assisted_match_rate: Number(assisted.toFixed(4)),
      exception_rate: Number((exceptionCount / recordCount).toFixed(4)),
      false_match_rate: 0,
      precision: 1,
      recall_on_resolvable: Number((1 - exceptionRate / 2).toFixed(4)),
      trap_capture_rate: 1,
      llm_rejection_rate: useLlm ? Number((0.35 + rng() * 0.2).toFixed(4)) : 0,
      throughput_records_per_sec: Number((900 + rng() * 400).toFixed(1)),
      llm_cost_usd_per_100: useLlm ? Number((0.018 + rng() * 0.006).toFixed(4)) : 0,
      llm_tokens_per_100: useLlm ? Math.round(1300 + rng() * 300) : 0,
      tier_counts: tierCounts,
      ...itc(recordCount, 0.9 + rng() * 0.2),
    }),
    pendingMatchCount: totalMatches(tierCounts),
    pendingExceptionCount: exceptionCount,
  });

  return runId;
}

/**
 * A run over uploaded files. `POST /api/runs` with `upload_ids`.
 *
 * `seed: -1` and `metrics: null` are not this mock being lazy -- they are the
 * contract. A merchant's own files have no ground truth, so the API scores
 * nothing and reports no seed, and a mock that invented a match rate here
 * would teach the console a state the real service never produces.
 */
export function createUploadRun(useLlm: boolean, recordCount: number): string {
  runCounter += 1;
  const runId = `run_up${hashString(`uploads:${recordCount}:${runCounter}`)
    .toString(16)
    .slice(0, 4)}`;

  const rng = prng(hashString(runId));
  const exceptionCount = Math.max(2, Math.round(recordCount * (0.01 + rng() * 0.02)));
  const matchCount = Math.max(3, Math.round((recordCount - exceptionCount) / 3.3));

  runs.unshift({
    summary: {
      run_id: runId,
      seed: -1,
      record_count: recordCount,
      state: "pending",
      created_at: new Date().toISOString(),
      match_count: 0,
      exception_count: 0,
      tier_confidence: TIER_CONFIDENCE,
      metrics: null,
    },
    startedAt: Date.now(),
    durationMs: 6_000,
    // Null at completion too, not merely while executing. That is the whole
    // point of this run shape.
    pendingMetrics: null,
    pendingMatchCount: matchCount,
    pendingExceptionCount: exceptionCount,
  });

  return runId;
}


/* ------------------------------------------------------------------ *
 * Subject records
 * ------------------------------------------------------------------ */

const ENTITIES = ["ACME RET", "NORTHWIND", "GLOBEX", "UMBRELLA", "SOYLENT"];

/** Deliberately messy. Double spaces are data, not a formatting accident. */
function narrationFor(rng: () => number, index: number, settlementId: string): string {
  const entity = ENTITIES[index % ENTITIES.length];
  switch (index % 5) {
    case 0:
      return `NEFT RAZORPAY ${settlementId} CREDIT`;
    case 1:
      return `RZPX*${entity}  RET PL`;
    case 2:
      return `NEFT UTR: SBIN${pad(226534120000 + index, 12)} RAZORPAY`;
    case 3:
      return "MISC CREDIT 00000";
    default:
      return `RZPX*${entity}   PL  ${pad(Math.floor(rng() * 999999), 6)}`;
  }
}

function bankLine(rng: () => number, index: number, settlementId: string): BankLine {
  const credit = 100_000 + Math.floor(rng() * 9_000_000);
  const hasUtr = index % 4 === 0;
  return {
    line_id: `BL-${pad(index, 6)}`,
    txn_date: isoDate(2 + (index % 21)),
    narration: narrationFor(rng, index, settlementId),
    credit,
    debit: null,
    balance: 40_000_000 + Math.floor(rng() * 60_000_000),
    utr: hasUtr ? `SBIN${pad(226534120000 + index, 12)}` : null,
  };
}

const PSP_TYPES: PSPTransaction["txn_type"][] = [
  "payment",
  "refund",
  "fee",
  "tax",
  "chargeback",
  "adjustment",
  "reserve",
];

function pspTransaction(
  rng: () => number,
  index: number,
  settlementId: string,
): PSPTransaction {
  const txnType = PSP_TYPES[index % PSP_TYPES.length];
  const magnitude = 5_000 + Math.floor(rng() * 900_000);
  // `amount` is SIGNED from the merchant's point of view: payments positive,
  // every deduction negative. Do not conflate with a bank line's unsigned
  // credit/debit magnitudes.
  const signed = txnType === "payment" || txnType === "adjustment" ? magnitude : -magnitude;
  const prefix =
    txnType === "payment"
      ? "pay"
      : txnType === "refund"
        ? "rfnd"
        : txnType === "chargeback"
          ? "cb"
          : txnType === "adjustment"
            ? "adj"
            : txnType === "fee"
              ? "fee"
              : txnType === "tax"
                ? "tax"
                : "rsv";
  const missingOrderRef = index % 3 === 0 || txnType === "fee" || txnType === "tax";
  return {
    txn_id: `${prefix}_${pad(index, 6)}`,
    txn_type: txnType,
    order_id: missingOrderRef ? null : `ORD-${pad(400000 + index, 6)}`,
    captured_at: isoDateTime(1 + (index % 21), index % 480),
    amount: signed,
    settlement_id: index % 11 === 0 ? null : settlementId,
    settled_at: index % 11 === 0 ? null : isoDate(3 + (index % 21)),
  };
}

const ORDER_STATUSES: Order["status"][] = [
  "paid",
  "refunded",
  "partially_refunded",
  "cancelled",
];

function order(rng: () => number, index: number): Order {
  return {
    order_id: `ORD-${pad(400000 + index, 6)}`,
    order_date: isoDate(index % 21),
    customer_ref: `CUST-${pad(1000 + (index % 720), 5)}`,
    gross_amount: 12_000 + Math.floor(rng() * 780_000),
    currency: "INR",
    status: ORDER_STATUSES[index % ORDER_STATUSES.length],
  };
}

/* ------------------------------------------------------------------ *
 * Exceptions
 * ------------------------------------------------------------------ */

const SUBJECT_TYPES: ReconExceptionDetail["subject_type"][] = [
  "bank_line",
  "psp_txn",
  "order",
];

/**
 * Which reason codes can legitimately be raised about which subject shape.
 * An ORPHAN_PSP_TXN on a bank line would be incoherent data, and incoherent
 * fixtures make a UI look wrong when it is right.
 */
const REASONS_BY_SUBJECT: Record<
  ReconExceptionDetail["subject_type"],
  ReasonCode[]
> = {
  bank_line: [
    "NO_SETTLEMENT_REF",
    "ORPHAN_BANK_LINE",
    "UNPARSEABLE_NARRATION",
    "AMBIGUOUS_MULTI_CANDIDATE",
    "AMOUNT_MISMATCH",
  ],
  psp_txn: [
    "ORPHAN_PSP_TXN",
    "DUPLICATE_PSP_TXN",
    "MISSING_ORDER_REF",
    "AMOUNT_MISMATCH",
  ],
  order: ["MISSING_ORDER_REF", "AMOUNT_MISMATCH"],
};

const CHECK_CYCLE: VerifierCheck[] = [
  "uniqueness",
  "arithmetic",
  "existence",
  "exclusivity",
  "causality",
];

/**
 * The settlement ids the mock knows about. `setl_A1` is the 63-order batch and
 * `setl_D4` the 4-order one -- the two shapes the netting diagram must read
 * correctly at.
 */
export const KNOWN_SETTLEMENTS = [
  "setl_A1",
  "setl_B7",
  "setl_C3",
  "setl_D4",
  "setl_K9",
  "setl_M2",
] as const;

function settlementFor(index: number): string {
  // Hashed rather than `index % length`: subject types cycle every 3, and a
  // plain modulo over 6 settlements aliases so that PSP subjects only ever
  // land on two of them.
  return KNOWN_SETTLEMENTS[hashString(`setl:${index}`) % KNOWN_SETTLEMENTS.length];
}

function hypothesisFor(
  reason: ReasonCode,
  settlementId: string,
  subjectId: string,
): string {
  switch (reason) {
    case "AMBIGUOUS_MULTI_CANDIDATE":
      return `${subjectId} settles ${settlementId}: both candidate batches reconstruct to the same net on the same date.`;
    case "AMOUNT_MISMATCH":
      return `${subjectId} settles ${settlementId} once a ₹0.50 rounding break in the fee leg is absorbed.`;
    case "MISSING_ORDER_REF":
      return `The missing order behind ${subjectId} is the only unclaimed order in ${settlementId} with a matching gross.`;
    case "ORPHAN_BANK_LINE":
      return `${subjectId} is the bank leg of ${settlementId}, whose credit was posted a day late.`;
    case "DUPLICATE_PSP_TXN":
      return `${subjectId} duplicates an earlier payment leg already claimed by ${settlementId}.`;
    default:
      return `${subjectId} belongs to ${settlementId} on the balance of the narration entity and the settlement date.`;
  }
}

function reasonFor(check: VerifierCheck | null, settlementId: string): string | null {
  if (check === null) return null;
  switch (check) {
    case "uniqueness":
      return `Two unclaimed settlements satisfy the arithmetic to the paise (${settlementId} and its neighbour on the same date). The verifier will not pick between them.`;
    case "arithmetic":
      return `Reconstructed net of ${settlementId} differs from the bank credit by 4,120 paise; the hypothesis does not close.`;
    case "existence":
      return `The hypothesis names a settlement id that is not present in this dataset.`;
    case "exclusivity":
      return `Two of the legs claimed are already accounted for by an earlier accepted match.`;
    case "causality":
      return `One claimed leg was captured after ${settlementId} settled, so it cannot belong to it.`;
  }
}

/**
 * What the analyst did on a run, derived from that run's OWN metrics.
 *
 * Exception rows have to agree with the scorecard beside them or the fixture
 * contradicts itself, and the analyst view exists precisely to tell three
 * zero-looking outcomes apart. A run reporting `tier_counts.LLM = 0` and
 * `llm_rejection_rate = 0.0` must therefore carry no hypotheses at all, and a
 * run reporting a rejection rate of 1.0 must carry no accepted verdict.
 */
type AnalystShape = {
  /** Whether any hypothesis was proposed on this run. */
  hypotheses: boolean;
  /** Whether any hypothesis was accepted. */
  acceptance: boolean;
};

function analystShapeOf(record: RunRecord | undefined): AnalystShape {
  const m = record?.summary.metrics ?? null;
  if (!m) return { hypotheses: false, acceptance: false };
  return {
    hypotheses: m.tier_counts.LLM > 0 || m.llm_rejection_rate > 0,
    acceptance: m.tier_counts.LLM > 0,
  };
}

function buildException(
  runId: string,
  index: number,
  shape: AnalystShape,
): ReconExceptionDetail {
  const rng = prng(hashString(`${runId}:${index}`));
  const subjectType = SUBJECT_TYPES[index % SUBJECT_TYPES.length];
  const candidates = REASONS_BY_SUBJECT[subjectType];
  const reason = candidates[index % candidates.length];
  const settlementId = settlementFor(index);

  let subject: SubjectRecord;
  let subjectId: string;
  let amount: number;

  if (subjectType === "bank_line") {
    const line = bankLine(rng, index, settlementId);
    subject = line;
    subjectId = line.line_id;
    amount = line.credit ?? -(line.debit ?? 0);
  } else if (subjectType === "psp_txn") {
    const txn = pspTransaction(rng, index, settlementId);
    subject = txn;
    subjectId = txn.txn_id;
    amount = txn.amount;
  } else {
    const ord = order(rng, index);
    subject = ord;
    subjectId = ord.order_id;
    amount = ord.gross_amount;
  }

  // Cycle the verdicts so all three appear, and so every one of the five
  // verifier checks is reachable. Rejections are the point, not an edge case.
  const bucket = index % 5;
  let verdict: ReconExceptionDetail["verifier_verdict"];
  let failedCheck: VerifierCheck | null;
  let hypothesis: string | null;

  if (!shape.hypotheses) {
    // No model ran on this run, so nothing about this subject was ever
    // proposed. `not_attempted` with a null hypothesis and a null failed_check
    // is the shape the contract gives that, and it is a different claim from
    // "a hypothesis was refused".
    verdict = "not_attempted";
    failedCheck = null;
    hypothesis = null;
  } else if (reason === "AMBIGUOUS_MULTI_CANDIDATE") {
    // The trap is unresolvable by construction, so a hypothesis about it can
    // only ever be refused on uniqueness. An accepted one here would mean
    // trap_capture_rate < 1.0, and the fixture would contradict its own
    // metrics.
    verdict = "rejected";
    failedCheck = "uniqueness";
    hypothesis = hypothesisFor(reason, settlementId, subjectId);
  } else if (bucket === 4) {
    verdict = "not_attempted";
    failedCheck = null;
    hypothesis = null;
  } else if (bucket === 3 && shape.acceptance) {
    verdict = "accepted";
    failedCheck = null;
    hypothesis = hypothesisFor(reason, settlementId, subjectId);
  } else {
    verdict = "rejected";
    failedCheck = CHECK_CYCLE[index % CHECK_CYCLE.length];
    hypothesis = hypothesisFor(reason, settlementId, subjectId);
  }

  return {
    // Zero-padded so lexicographic order is numeric order: the contract's
    // stable ORDER BY exception_id, with no page showing a row twice.
    exception_id: `exc_${runId}_${pad(index, 6)}`,
    subject_type: subjectType,
    subject_id: subjectId,
    reason_code: reason,
    amount,
    llm_hypothesis: hypothesis,
    verifier_verdict: verdict,
    verifier_reason:
      verdict === "rejected"
        ? reasonFor(failedCheck, settlementId)
        : verdict === "accepted"
          ? `Reconstructed net of ${settlementId} equals the bank credit exactly, and every claimed leg is unclaimed and causal.`
          : null,
    failed_check: failedCheck,
    subject,
  };
}

const exceptionCache = new Map<string, ReconExceptionDetail[]>();

export function exceptionsFor(runId: string): ReconExceptionDetail[] {
  const record = findRun(runId);
  const count = record ? record.summary.exception_count : 0;

  // A run in flight reports 0 exceptions and then jumps to its real count when
  // it completes, so the cache is keyed on the count as well as the id.
  const cached = exceptionCache.get(runId);
  if (cached && cached.length === count) return cached;

  const shape = analystShapeOf(record);
  const rows: ReconExceptionDetail[] = [];
  for (let i = 0; i < count; i += 1) rows.push(buildException(runId, i, shape));
  exceptionCache.set(runId, rows);
  return rows;
}

export function findException(
  exceptionId: string,
): { runId: string; detail: ReconExceptionDetail } | undefined {
  // exception_id is `exc_<run_id>_<index>` by construction.
  const match = /^exc_(.+)_(\d{6})$/.exec(exceptionId);
  if (!match) return undefined;
  const [, runId, indexStr] = match;
  const rows = exceptionsFor(runId);
  const index = Number(indexStr);
  const detail = rows[index];
  return detail ? { runId, detail } : undefined;
}

/* ------------------------------------------------------------------ *
 * Audit trails
 * ------------------------------------------------------------------ */

export function auditTrailFor(
  runId: string,
  detail: ReconExceptionDetail,
): AuditEntry[] {
  const rng = prng(hashString(`${detail.exception_id}:trail`));
  const settlementId = settlementFor(Number(detail.exception_id.slice(-6)));
  const entries: Omit<AuditEntry, "entry_id" | "run_id" | "sequence">[] = [
    {
      subject_id: detail.subject_id,
      stage: "ingest",
      actor: "deterministic",
      rule: "csv.read",
      evidence: `Parsed ${detail.subject_type} ${detail.subject_id} with amount as integer paise.`,
      confidence: 1,
    },
    {
      subject_id: detail.subject_id,
      stage: "canonicalize",
      actor: "deterministic",
      rule: "narration.canonicalize",
      evidence:
        detail.subject_type === "bank_line"
          ? `settlement_id=${(detail.subject as BankLine).narration.includes("setl_") ? settlementId : "null"} utr=${(detail.subject as BankLine).utr ?? "null"}`
          : "not a bank line; canonicalisation skipped",
      confidence: 1,
    },
    {
      subject_id: detail.subject_id,
      stage: "match",
      actor: "deterministic",
      rule: "T0.settlement_reference",
      evidence: "No settlement reference in narration; T0 declines.",
      confidence: 0,
    },
    {
      subject_id: detail.subject_id,
      stage: "match",
      actor: "deterministic",
      rule: "T2.batch_reconstruction",
      evidence: `Candidate set size ${detail.reason_code === "AMBIGUOUS_MULTI_CANDIDATE" ? 2 : 0}; more than one candidate means match nothing.`,
      confidence: 0,
    },
    {
      subject_id: detail.subject_id,
      stage: "match",
      actor: "deterministic",
      rule: "T3.tolerance",
      evidence: `delta=${Math.floor(rng() * 400)} exceeds the 100 paise tolerance; T3 declines.`,
      confidence: 0,
    },
  ];

  if (detail.llm_hypothesis !== null) {
    entries.push({
      subject_id: detail.subject_id,
      stage: "llm",
      actor: "llm",
      rule: "analyst.propose",
      evidence: detail.llm_hypothesis,
      confidence: Number((0.55 + rng() * 0.4).toFixed(2)),
    });
    entries.push({
      subject_id: detail.subject_id,
      stage: "verify",
      actor: "verifier",
      rule:
        detail.failed_check !== null
          ? `verifier.${detail.failed_check}`
          : "verifier.all_checks",
      evidence:
        detail.verifier_reason ??
        "All five checks passed; hypothesis accepted as an assisted match.",
      confidence: detail.verifier_verdict === "accepted" ? 1 : 0,
    });
  }

  return entries.map((entry, i) => ({
    ...entry,
    entry_id: `aud_${detail.exception_id}_${pad(i, 3)}`,
    run_id: runId,
    sequence: i,
  }));
}

export function auditedException(exceptionId: string): ReconExceptionAudited | undefined {
  const found = findException(exceptionId);
  if (!found) return undefined;
  return {
    ...found.detail,
    audit_trail: auditTrailFor(found.runId, found.detail),
  };
}

/* ------------------------------------------------------------------ *
 * Batch netting -- the diagram's data
 * ------------------------------------------------------------------ */

/** Order counts pinned for the two shapes the diagram must read at. */
const PINNED_ORDER_COUNTS: Record<string, number> = {
  setl_A1: 63,
  setl_D4: 4,
  setl_B7: 18,
  setl_C3: 7,
  setl_K9: 2,
  setl_M2: 1,
};

export function batchNetting(runId: string, settlementId: string): BatchNetting {
  const rng = prng(hashString(`${runId}:${settlementId}`));
  const orderCount =
    PINNED_ORDER_COUNTS[settlementId] ?? 1 + (hashString(settlementId) % 40);

  const orders: BatchNettingOrderLine[] = [];
  for (let i = 0; i < orderCount; i += 1) {
    orders.push({
      order_id: `ORD-${pad(400000 + hashString(`${settlementId}:${i}`) % 90000, 6)}`,
      gross_amount: 18_000 + Math.floor(rng() * 260_000),
    });
  }

  // Fee base is the settlement's OWN payment legs only. Refunds and
  // chargebacks reduce the net, never the fee base -- so `fees` is not 2.36%
  // of the line above it once a refund is in the batch. That is correct.
  const gross = orders.reduce((sum, o) => sum + o.gross_amount, 0);
  const fees = pctOf(gross, MDR_BPS);
  const tax = pctOf(fees, GST_BPS);
  const refunds = orderCount > 8 ? 89_000 + Math.floor(rng() * 40_000) : 0;
  const holds = orderCount > 30 ? 50_000 : 0;

  // ONE BATCH HERE DOES NOT ADD UP, ON PURPOSE.
  //
  // `net` is THE BANK CREDIT, not the reconstruction, and T3 is exactly the
  // rung where those two differ: it accepts a batch whose reconstruction is
  // within the matcher's 100-paise tolerance of the credit. `setl_D4` is the
  // rounding-break shape ARCHITECTURE.md works through -- the settlement
  // reconstructs 50 paise above the credit, T0 declines on the residual, and
  // T3 takes it at confidence 0.80 with the delta recorded.
  //
  // Every other fixture batch closes exactly, so without this one the
  // renderer's residual path would never run and a UI that silently balanced
  // the columns would look correct in every screenshot.
  const residual = settlementId === "setl_D4" ? 50 : 0;
  const net = gross - fees - tax - refunds - holds - residual;

  const psp_txn_ids = orders.map((_, i) => `pay_${pad(hashString(`${settlementId}:p:${i}`) % 999999, 6)}`);
  psp_txn_ids.push(`fee_${settlementId.slice(5)}`, `tax_${settlementId.slice(5)}`);
  if (refunds > 0) psp_txn_ids.push(`rfnd_${settlementId.slice(5)}`);
  if (holds > 0) psp_txn_ids.push(`cb_${settlementId.slice(5)}`);

  return {
    settlement_id: settlementId,
    bank_line_id: `BL-${pad(hashString(settlementId) % 999999, 6)}`,
    orders,
    psp_txn_ids,
    gross,
    fees,
    tax,
    refunds,
    holds,
    net,
    tier: residual > 0 ? "T3" : orderCount > 1 ? "T2" : "T1",
    evidence:
      residual > 0
        ? [
            `${orderCount} payment legs reconstruct to ${net + residual} paise.`,
            `MDR ${MDR_BPS} bps on the payment-leg gross only; GST ${GST_BPS} bps on the MDR.`,
            `[T0:reference-hit-arithmetic-declined] reference ${settlementId} hit, but it reconstructs to ${net + residual} paise against credit ${net} -- residual delta=${residual} paise (net - credit). A settlement id proves identity, not arithmetic, so T0 declines and the line falls through.`,
            `[T3:tolerance] delta=${residual} paise is within the 100 paise tolerance; matched at confidence 0.80.`,
          ]
        : [
            `${orderCount} payment legs reconstruct to a net of ${net} paise.`,
            `MDR ${MDR_BPS} bps on the payment-leg gross only; GST ${GST_BPS} bps on the MDR.`,
            `Bank credit matched to the paise; single candidate in the ±1 day window.`,
          ],
  };
}

/* ------------------------------------------------------------------ *
 * Matches
 * ------------------------------------------------------------------ */

export function matchDetail(matchId: string): MatchDetail {
  const settlementId = matchId.startsWith("mat_")
    ? (KNOWN_SETTLEMENTS[hashString(matchId) % KNOWN_SETTLEMENTS.length] as string)
    : "setl_A1";
  const netting = batchNetting("run_5f21a9", settlementId);
  const rng = prng(hashString(matchId));

  const subject: BankLine = {
    line_id: netting.bank_line_id,
    txn_date: isoDate(3),
    narration: `NEFT RAZORPAY ${settlementId} CREDIT`,
    credit: netting.net,
    debit: null,
    balance: 40_000_000 + Math.floor(rng() * 60_000_000),
    utr: `SBIN${pad(226534120000 + (hashString(matchId) % 9999), 12)}`,
  };

  return {
    match_id: matchId,
    bank_line_id: netting.bank_line_id,
    settlement_id: settlementId,
    psp_txn_ids: netting.psp_txn_ids,
    order_ids: netting.orders.map((o) => o.order_id),
    gross: netting.gross,
    fees: netting.fees,
    tax: netting.tax,
    refunds: netting.refunds,
    holds: netting.holds,
    net: netting.net,
    tier: netting.tier,
    confidence: netting.tier === "T2" ? 0.99 : 0.95,
    evidence: netting.evidence,
    subject,
    audit_trail: [
      {
        entry_id: `aud_${matchId}_000`,
        run_id: "run_5f21a9",
        subject_id: subject.line_id,
        stage: "match",
        actor: "deterministic",
        rule: "T2.batch_reconstruction",
        evidence: `Reconstructed net ${netting.net} equals bank credit ${netting.net}.`,
        confidence: 0.99,
        sequence: 0,
      },
    ],
  };
}

/* ------------------------------------------------------------------ *
 * Drift: two runs compared
 * ------------------------------------------------------------------ */

/**
 * A stand-in for `core/drift/compare.py`, holding to its rules exactly.
 *
 * The thresholds below are the engine's own named constants and are reproduced
 * rather than invented, because a mock that flagged different moves than the
 * API would teach the UI a materiality rule the real service does not have.
 *
 *   RATE_MATERIAL_DELTA      0.01 absolute, for the bounded rates
 *   EXACT_METRICS            any change at all -- these three have a known
 *                            correct value, and 0.0 -> 0.004 on the false-match
 *                            rate is four wrong matches hiding under 0.01
 *   MAGNITUDE_MATERIAL_RATIO 0.05 relative, for the unbounded magnitudes;
 *                            any appearance from a zero baseline
 *   NEVER_MATERIAL           throughput, which is wall clock on shared hardware
 */
const RATE_MATERIAL_DELTA = 0.01;
const MAGNITUDE_MATERIAL_RATIO = 0.05;
const EXACT_METRICS = new Set([
  "false_match_rate",
  "precision",
  "trap_capture_rate",
]);
const MAGNITUDE_METRICS = new Set([
  "llm_cost_usd_per_100",
  "llm_tokens_per_100",
  "itc_substantiated_paise",
  "itc_at_risk_paise",
  "itc_variance_paise",
]);
const NEVER_MATERIAL_METRICS = new Set(["throughput_records_per_sec"]);

/** Declaration order of `Metrics`, which is the order moves are reported in. */
const COMPARED_METRICS: readonly (keyof Metrics)[] = [
  "auto_match_rate",
  "assisted_match_rate",
  "exception_rate",
  "false_match_rate",
  "precision",
  "recall_on_resolvable",
  "trap_capture_rate",
  "llm_rejection_rate",
  "throughput_records_per_sec",
  "llm_cost_usd_per_100",
  "llm_tokens_per_100",
  "itc_substantiated_paise",
  "itc_at_risk_paise",
  "itc_variance_paise",
];

function isMaterial(metric: string, before: number, after: number): boolean {
  if (NEVER_MATERIAL_METRICS.has(metric)) return false;
  if (before === after) return false;
  if (EXACT_METRICS.has(metric)) return true;
  if (MAGNITUDE_METRICS.has(metric)) {
    if (before === 0) return true;
    return Math.abs(after - before) / Math.abs(before) >= MAGNITUDE_MATERIAL_RATIO;
  }
  return Math.abs(after - before) >= RATE_MATERIAL_DELTA;
}

function reasonCensus(runId: string): Record<string, number> {
  const census: Record<string, number> = {};
  for (const row of exceptionsFor(runId)) {
    census[row.reason_code] = (census[row.reason_code] ?? 0) + 1;
  }
  return census;
}

/**
 * The baseline the API picks when no `against` is given: the immediately
 * previous COMPLETED run on the same dataset. These fixtures carry no dataset
 * link, so "same dataset" is approximated by the pair the real store would have
 * grouped -- same seed and same record count -- and the newest such run created
 * before this one wins.
 */
function previousCompletedRun(current: RunSummary): RunSummary | null {
  const earlier = runs
    .map((r) => r.summary)
    .filter(
      (s) =>
        s.run_id !== current.run_id &&
        s.state === "completed" &&
        s.seed === current.seed &&
        s.record_count === current.record_count &&
        s.created_at < current.created_at,
    )
    .sort((a, b) => (a.created_at < b.created_at ? 1 : -1));
  return earlier[0] ?? null;
}

export type DriftOutcome =
  | { kind: "ok"; report: DriftReport }
  | { kind: "not_found"; detail: string }
  | { kind: "conflict"; detail: string };

export function driftReport(runId: string, against: string | null): DriftOutcome {
  const current = findRun(runId);
  if (!current) return { kind: "not_found", detail: `no run with id '${runId}'` };

  let baseline: RunSummary | null;
  if (against === null) {
    baseline = previousCompletedRun(current.summary);
    if (baseline === null) {
      return {
        kind: "not_found",
        detail:
          `run '${runId}' has no earlier completed run on its dataset to ` +
          "compare against; pass ?against=<run_id> to choose one",
      };
    }
  } else {
    const other = findRun(against);
    if (!other) return { kind: "not_found", detail: `no run with id '${against}'` };
    baseline = other.summary;
  }

  if (baseline.record_count !== current.summary.record_count) {
    return {
      kind: "conflict",
      detail:
        `run '${baseline.run_id}' ran on ${baseline.record_count} records and ` +
        `run '${current.summary.run_id}' on ${current.summary.record_count}; ` +
        "every rate is computed over a different denominator and every rupee " +
        "figure over a different scale, so the two are not comparable",
    };
  }

  const before = baseline.metrics;
  const after = current.summary.metrics;
  if (before === null || after === null) {
    const which = before === null ? baseline.run_id : current.summary.run_id;
    return {
      kind: "conflict",
      detail: `run '${which}' has no metrics, so there is nothing to compare`,
    };
  }

  const moves: MetricMove[] = COMPARED_METRICS.map((metric) => {
    const b = before[metric] as number;
    const a = after[metric] as number;
    return {
      metric,
      before: b,
      after: a,
      delta: a - b,
      material: isMaterial(metric, b, a),
    };
  });

  const baseCensus = reasonCensus(baseline.run_id);
  const currentCensus = reasonCensus(current.summary.run_id);
  const codes = [
    ...new Set([...Object.keys(baseCensus), ...Object.keys(currentCensus)]),
  ].sort();

  const reason_code_moves: ReasonCodeMove[] = codes
    .map((code) => ({
      reason_code: code,
      before: baseCensus[code] ?? 0,
      after: currentCensus[code] ?? 0,
      appeared: (baseCensus[code] ?? 0) === 0 && (currentCensus[code] ?? 0) > 0,
    }))
    .filter((move) => move.before !== move.after);

  return {
    kind: "ok",
    report: {
      baseline_run_id: baseline.run_id,
      current_run_id: current.summary.run_id,
      moves,
      reason_code_moves,
      // This endpoint runs no model. The contract says so, and a mock that
      // invented prose here would teach the UI to expect something the real
      // service never sends.
      narrative: null,
    },
  };
}

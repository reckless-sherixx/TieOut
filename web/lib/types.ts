/**
 * Convenience re-exports of the generated OpenAPI schema types
 * (lib/api-types.ts, generated from api/openapi.yaml — never hand-edited).
 *
 * Everything the UI needs comes from here rather than reaching into
 * `components["schemas"][...]` inline at every call site.
 */
import type { components } from "./api-types";

export type Money = components["schemas"]["Money"];
export type ReasonCode = components["schemas"]["ReasonCode"];
export type VerifierCheck = components["schemas"]["VerifierCheck"];
export type RunState = components["schemas"]["RunState"];

export type Order = components["schemas"]["Order"];
export type PSPTransaction = components["schemas"]["PSPTransaction"];
export type BankLine = components["schemas"]["BankLine"];
export type SubjectRecord = components["schemas"]["SubjectRecord"];

export type Metrics = components["schemas"]["Metrics"];
export type RunSummary = components["schemas"]["RunSummary"];
export type MatchGroup = components["schemas"]["MatchGroup"];
export type ReconException = components["schemas"]["ReconException"];
export type AuditEntry = components["schemas"]["AuditEntry"];

export type ReconExceptionDetail = components["schemas"]["ReconExceptionDetail"];
export type ReconExceptionAudited = components["schemas"]["ReconExceptionAudited"];
export type MatchDetail = components["schemas"]["MatchDetail"];

export type BatchNettingOrderLine = components["schemas"]["BatchNettingOrderLine"];
export type BatchNetting = components["schemas"]["BatchNetting"];

export type Pagination = components["schemas"]["Pagination"];
export type PaginatedReconExceptions = components["schemas"]["PaginatedReconExceptions"];

/**
 * Drift: what changed between two runs of the same dataset shape.
 *
 * `narrative` is `string | null` on the contract and is null on every response
 * this API produces, because the endpoint runs no model. The UI renders it when
 * it is there and renders nothing at all when it is not — it never writes prose
 * of its own into that slot, which would be exactly the thing the contract
 * separates `narrative` from `material` to prevent.
 */
export type MetricMove = components["schemas"]["MetricMove"];
export type ReasonCodeMove = components["schemas"]["ReasonCodeMove"];
export type DriftReport = components["schemas"]["DriftReport"];
/** The 409 body. Same shape as NotFoundError, named separately by the contract. */
export type ConflictError = components["schemas"]["ConflictError"];

export type GenerateDatasetRequest = components["schemas"]["GenerateDatasetRequest"];
export type DatasetGenerated = components["schemas"]["DatasetGenerated"];
export type CreateRunRequest = components["schemas"]["CreateRunRequest"];
export type RunCreated = components["schemas"]["RunCreated"];
export type RunStatus = components["schemas"]["RunStatus"];

export type SubjectType = ReconException["subject_type"];

/**
 * A SubjectRecord paired with the tag that identifies it.
 *
 * `SubjectRecord` is a bare union of the three input shapes with no
 * discriminator inside the records themselves, so the only reliable tag is the
 * sibling `subject_type` field. This turns that pair into a discriminated
 * union a `switch` can narrow cleanly — nothing in the UI ever sniffs for a
 * field to work out what a subject is.
 */
export type TaggedSubject =
  | { type: "order"; record: Order }
  | { type: "psp_txn"; record: PSPTransaction }
  | { type: "bank_line"; record: BankLine };

export function tagSubject(
  subjectType: SubjectType,
  subject: SubjectRecord,
): TaggedSubject {
  switch (subjectType) {
    case "order":
      return { type: "order", record: subject as Order };
    case "psp_txn":
      return { type: "psp_txn", record: subject as PSPTransaction };
    case "bank_line":
      return { type: "bank_line", record: subject as BankLine };
  }
}

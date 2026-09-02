"""The upload pipeline: bytes in, canonical records and quarantine out.

`api/jobs.py` is the run job; this is the ingest job, and it is separate for the
same reason: `api/routes.py` validates a request and serialises a model, and the
sequence below is neither. It is also the only place in `api/` that touches the
blob store, so "where does an uploaded file go" has one answer.

**The order of the four steps is the whole design, and one of them comes first
for a reason that is not obvious.**

1. **Hash, then look up.** The content digest is computed before anything else
   and `Repo.upload_for_content` is asked whether this org already holds it. A
   re-upload of January's settlement report therefore costs one SELECT: no
   temporary file, no sniff, no parse, no second blob, no duplicate rows. That
   is spec 2026-08-30 §3 A3 read literally -- *same file content, same result,
   no duplicates* -- and doing the lookup last, after a parse whose output is
   then thrown away, would satisfy the letter of it and none of the point.

2. **Detect, and refuse rather than guess.** `core.adapters.registry.detect`
   owns the decision and this module does not restate its threshold; it only
   catches the two refusals and turns them into a 422 that names every
   candidate and its confidence. `sniff_scores` supplies that list so the
   scores in the error body are the scores the decision was made on.

3. **Parse.** The adapter's own `parse`, which never raises past a row: a
   malformed row is a `QuarantinedRow` and the file keeps going.

4. **Store the bytes, then the rows.** The blob lands *after* detection
   succeeds. A file this API refused is not retained: there would be no upload
   row pointing at it, so it could never be listed, reviewed, exported or
   erased -- an unreferenced copy of a merchant's data with no way to find it
   again is the one thing a retention policy cannot describe (COMPLIANCE.md).
   The 422 already tells the merchant everything the file could have told them.

**A temporary plaintext file exists, briefly, and it has to.** Every adapter
takes a path -- they stream, and a 400 MB bank export must not be held in
memory twice -- while the blob store holds an encrypted envelope that no
adapter could read. So the bytes are written to a scratch file inside the
uploads root, parsed, and removed in a `finally`. It is inside that root rather
than the system temp directory so the operator's own disk-encryption and
retention arrangements cover it, and it is removed whether the parse succeeded,
failed or raised.

No clock is read here either. `uploaded_at` is stamped by the caller in
`api/routes.py` from `api/jobs.utc_now`, the same boundary that stamps a run's
`created_at`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from core.adapters import registry
from core.adapters.base import (
    DETECTION_THRESHOLD,
    FormatDetectionError,
    UndecodableFileError,
)
from core.store.blobstore import BlobStore
from core.store.repo import Repo, UploadedRow, UploadIngestion

from api import settings

#: The two ways a file can fail to be a format this build reads. They are
#: distinct because the fixes are: "your export is not one we recognise" against
#: "these bytes are not text at all", which is usually a spreadsheet or a zip
#: renamed to `.csv`.
UNRECOGNISED_FORMAT = "UNRECOGNISED_FORMAT"
UNDECODABLE_FILE = "UNDECODABLE_FILE"

#: A ceiling on one upload, in bytes. Not a security control -- the ASGI server
#: and any reverse proxy in front of it have their own -- but a legible refusal
#: is better than an out-of-memory kill, and 64 MB is comfortably past the
#: largest real settlement export (a 40,000-row month is under 10 MB).
MAX_UPLOAD_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True)
class FormatCandidate:
    """One adapter's confidence in a file that was refused."""

    format_id: str
    confidence: float


class UploadRefused(Exception):
    """The file could not be turned into anything, and the caller gets a 422.

    Carries the structured facts the contract's `UploadRefusedError` declares:
    which of the two refusals it was, and every candidate format with the score
    it actually got. A merchant reading "no adapter recognised this file" and a
    merchant reading "the Razorpay adapter scored 0.42 and the threshold is
    0.60" are in very different positions to fix it.

    **It never quotes the file.** The message names formats, scores and the
    threshold, and no byte of the merchant's data goes into an error body --
    the quarantine view is behind a session for exactly that reason and an
    error response is not (spec 2026-08-30 §5).
    """

    def __init__(
        self,
        reason: str,
        detail: str,
        candidates: tuple[FormatCandidate, ...] = (),
    ) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail
        self.candidates = candidates

    def body(self) -> dict:
        """The 422 body, exactly as `api/openapi.yaml` declares it."""
        return {
            "detail": self.detail,
            "reason": self.reason,
            "threshold": DETECTION_THRESHOLD,
            "candidates": [
                {"format_id": c.format_id, "confidence": c.confidence}
                for c in self.candidates
            ],
        }


class UploadTooLarge(Exception):
    """More bytes than `MAX_UPLOAD_BYTES`. Rendered as a 413."""


def blob_store() -> BlobStore:
    """The store this deployment writes uploads to.

    Constructed per call rather than cached: it holds an AES key and a path,
    both of which are configuration read at call time, and a cached instance
    would keep serving the key a test had just replaced. It opens no
    connection and does no I/O beyond creating its root, so there is nothing to
    amortise.
    """
    return BlobStore(settings.uploads_dir(), key=settings.blob_key())


def ingest_upload(
    repo: Repo, *, filename: str, payload: bytes, uploaded_at: datetime
) -> UploadIngestion:
    """Run the four steps above and return what the store decided.

    Raises `UploadRefused` for a file no adapter recognised and
    `UploadTooLarge` for one past the ceiling. Every other outcome -- including
    a file whose every row was quarantined, and one that parsed to nothing at
    all -- is a persisted upload with a state that says which it was.
    """
    if len(payload) > MAX_UPLOAD_BYTES:
        raise UploadTooLarge(
            f"the file is {len(payload)} bytes; this API accepts at most "
            f"{MAX_UPLOAD_BYTES}"
        )

    digest = BlobStore.address(payload)
    already = repo.upload_for_content(digest)
    if already is not None:
        # Step 1. Nothing is written and nothing is parsed: the bytes are the
        # identity, and this org already has them under a name it holds.
        return UploadIngestion(upload=already, already_ingested=True)

    scratch = _scratch_path(digest)
    try:
        scratch.parent.mkdir(parents=True, exist_ok=True)
        scratch.write_bytes(payload)
        adapter, confidence = _detect(scratch)
        result = adapter.parse(scratch)
    finally:
        # Whatever happened, the plaintext copy goes. A refused upload must not
        # leave one behind, and neither must a parse that raised.
        try:
            os.unlink(scratch)
        except OSError:
            pass

    stored = blob_store().put(payload)
    if stored != digest:  # pragma: no cover -- the store addresses on plaintext
        raise RuntimeError(
            "the blob store returned an address that is not the content digest"
        )

    return repo.record_upload(
        upload_id=f"upl-{uuid4().hex[:12]}",
        filename=filename,
        content_sha256=digest,
        byte_size=len(payload),
        format_id=result.format_id,
        format_version=result.format_version,
        confidence=confidence,
        encoding=result.encoding,
        uploaded_at=uploaded_at,
        records=result.records,
        row_hashes=result.row_hashes,
        quarantined=[
            UploadedRow(
                row_number=row.row_number,
                raw=row.raw,
                reason=row.reason.value,
                detail=row.detail,
            )
            for row in result.quarantined
        ],
        skipped_rows=result.skipped_rows,
    )


def _scratch_path(digest: str) -> Path:
    """Where the plaintext copy lives while an adapter reads it.

    Named for the content and a nonce, under the uploads root: two concurrent
    uploads of the same file cannot collide on it, and it never lands in the
    blob store's own fan-out where a `.tmp` suffix already means something.
    """
    return settings.uploads_dir() / "incoming" / f"{digest}.{uuid4().hex[:8]}.part"


def _detect(path: Path) -> tuple[object, float]:
    """The adapter that reads this file and the score it won with.

    **The threshold is not restated here.** `registry.detect` owns the decision
    and raises the two refusals; this scores the file once, hands the same
    scores to `detect` so the two cannot disagree, and turns a refusal into an
    `UploadRefused` carrying that identical list.

    The confidence is reported alongside the detected format because a merchant
    whose export scored 0.62 against a 0.60 threshold and a merchant whose
    export scored 1.00 are in different positions, and a screen that showed
    only the format name would tell them the same thing.
    """
    try:
        scored = registry.sniff_scores(path)
    except UndecodableFileError as error:
        raise UploadRefused(
            UNDECODABLE_FILE,
            f"{error} Detection reads bytes, so a spreadsheet or an archive "
            f"renamed to .csv fails here rather than parsing into nonsense.",
        ) from error

    candidates = tuple(
        FormatCandidate(format_id=adapter.format_id, confidence=score)
        for adapter, score in scored
    )
    try:
        adapter = registry.detect(path, adapters=[a for a, _ in scored])
    except FormatDetectionError as error:
        raise UploadRefused(
            UNRECOGNISED_FORMAT, str(error), candidates=candidates
        ) from error

    return adapter, next(
        score for candidate, score in scored if candidate is adapter
    )

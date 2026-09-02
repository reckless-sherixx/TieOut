"""Which adapter reads this file -- decided by header shape, never by name.

`detect` asks every registered adapter how confident it is in the first
`SNIFF_BYTES` of the file and takes the highest score. It refuses in two cases,
and refusing is the feature:

* **nothing cleared the threshold.** A file whose header half-resembles three
  layouts is not a file to guess at. The error names every candidate and its
  score, so the answer to "why did it not read my export" is in the message.
* **the top two tied.** Two adapters equally sure is a registry bug or a genuine
  ambiguity; either way the correct output is a question, not a parse.

Filenames never enter this. `sniff` takes `head: bytes` and has no other
parameter, so an adapter physically cannot consult the name. That matters more
than it sounds: merchants rename exports constantly, and `hdfc_aug.csv` holding
an ICICI statement is a Tuesday.

`detect` is the question "which adapter is this"; `ingest` is the upload path.
The second one never raises, because whatever a merchant sent has to be
recorded and a traceback is not a record.
"""

from __future__ import annotations

from pathlib import Path

from core.adapters.base import (
    DETECTION_THRESHOLD,
    SNIFF_BYTES,
    AdapterResult,
    FormatDetectionError,
    QuarantinedRow,
    QuarantineReason,
    SourceAdapter,
    UndecodableFileError,
    decode_bytes,
    sha256_bytes,
)


def default_adapters() -> tuple[SourceAdapter, ...]:
    """Every adapter this build ships, in no significant order.

    Imported inside the function rather than at module scope so that
    `core.adapters.registry` stays importable while an adapter module is being
    written, and so an adapter can import from `base` without a cycle through
    here.
    """
    from core.adapters.bank_hdfc import HDFCStatementAdapter
    from core.adapters.bank_slice import SlicePDFStatementAdapter
    from core.adapters.cod_remittance import CODRemittanceAdapter
    from core.adapters.bank_icici import ICICIStatementAdapter
    from core.adapters.mt940 import MT940Adapter
    from core.adapters.orders_shopify import ShopifyOrdersAdapter
    from core.adapters.razorpay_settlement import RazorpaySettlementAdapter

    return (
        RazorpaySettlementAdapter(),
        HDFCStatementAdapter(),
        ICICIStatementAdapter(),
        MT940Adapter(),
        ShopifyOrdersAdapter(),
        CODRemittanceAdapter(),
        SlicePDFStatementAdapter(),
    )


def adapters() -> tuple[SourceAdapter, ...]:
    """Alias for `default_adapters`, for callers that only want to list them."""
    return default_adapters()


def read_head(path: Path | str, size: int = SNIFF_BYTES) -> bytes:
    with open(path, "rb") as handle:
        return handle.read(size)


def sniff_scores(
    path: Path | str,
    *,
    adapters: list[SourceAdapter] | tuple[SourceAdapter, ...] | None = None,
) -> list[tuple[SourceAdapter, float]]:
    """Every registered adapter's confidence in `path`, best first.

    Split out of `detect` so a caller that has to EXPLAIN a refusal can list
    the candidates it refused between without re-deriving them -- the upload
    path answers a low-confidence sniff with a 422 naming every format and its
    score, and building that list a second time would let the two disagree.
    `detect` still owns the decision; this only owns the arithmetic behind it.

    Raises `UndecodableFileError` for a file that is not text at all, for the
    same reason `detect` does: an unreadable file is not an unrecognised
    format, and folding the two together would report a row of zero
    confidences for a file no adapter was ever shown.

    **Except when a format is genuinely binary.** A PDF statement is not text
    and never will be, so "these bytes do not decode" stopped being the same
    fact as "this file is not a statement" the moment the first PDF adapter
    landed. An adapter declares `reads_binary = True` when it reads a container
    rather than an export, and only those adapters are shown bytes that failed
    to decode. If none of them recognises the file, the original
    `UndecodableFileError` is raised exactly as before -- a spreadsheet renamed
    to `.csv` still gets the answer it always got, and a text-only adapter is
    still never handed a NUL byte to sniff.
    """
    candidates = tuple(adapters) if adapters is not None else default_adapters()
    head = read_head(path)

    def scored(subset: tuple[SourceAdapter, ...]) -> list[tuple[SourceAdapter, float]]:
        return [(adapter, float(adapter.sniff(head))) for adapter in subset]

    try:
        decode_bytes(head)
    except UndecodableFileError:
        binary_capable = tuple(
            adapter for adapter in candidates if getattr(adapter, "reads_binary", False)
        )
        results = scored(binary_capable)
        if not any(score > 0.0 for _adapter, score in results):
            raise
        # The text-only adapters are reported at zero rather than omitted, so a
        # refusal message still names every format that was considered.
        results += [
            (adapter, 0.0) for adapter in candidates if adapter not in binary_capable
        ]
        return sorted(results, key=lambda pair: pair[1], reverse=True)

    return sorted(scored(candidates), key=lambda pair: pair[1], reverse=True)


def detect(
    path: Path | str,
    *,
    adapters: list[SourceAdapter] | tuple[SourceAdapter, ...] | None = None,
    threshold: float = DETECTION_THRESHOLD,
) -> SourceAdapter:
    """Return the one adapter that recognises `path`, or refuse loudly.

    Raises `UndecodableFileError` if the file is not text at all -- that is a
    file-level quarantine and a different fact from "no adapter recognised the
    header", so it gets a different exception rather than being folded into a
    confusing zero-confidence report.
    """
    scored = sniff_scores(path, adapters=adapters)
    if not scored:
        raise FormatDetectionError("no adapters are registered, so nothing can be detected")

    report = ", ".join(f"{adapter.format_id}={score:.2f}" for adapter, score in scored)
    best_adapter, best_score = scored[0]

    if best_score < threshold:
        raise FormatDetectionError(
            f"no adapter recognised this file's header with confidence >= "
            f"{threshold:.2f}; candidates were {report}. Detection is by header "
            f"shape, so check the first line of the file, not its name."
        )
    if len(scored) > 1 and scored[1][1] == best_score:
        tied = [adapter.format_id for adapter, score in scored if score == best_score]
        raise FormatDetectionError(
            f"tie at confidence {best_score:.2f} between {tied}; refusing to "
            f"guess. Candidates were {report}."
        )
    return best_adapter


#: `format_id` used by `ingest` when detection itself failed. It is not an
#: adapter and never will be: a file nothing could read still needs somewhere to
#: put the fact that it arrived, and a caller filtering on real format ids must
#: not accidentally pick these up.
UNREADABLE_FORMAT_ID = "unreadable"


def ingest(path: Path | str) -> AdapterResult:
    """Detect and parse, returning a result in every case. Never raises.

    `detect` and `parse` both raise on a file-level failure, and that is right
    for them -- a caller that asked "which adapter is this" deserves an answer
    or an exception, not a silent empty result. But the upload path has a
    different job: whatever the merchant sent, something must be recorded, and
    a traceback is not a record. So this wraps both and turns the two
    file-level failures into what row-level failures already are -- a
    `QuarantinedRow` with a reason a review screen can group on.

    `row_number=1` on those records is the honest answer: the defect is the
    file, and line 1 is where a human starts looking.
    """
    path = Path(path)
    try:
        raw = path.read_bytes()
    except OSError as error:
        return _unreadable(
            "", QuarantineReason.UNDECODABLE_FILE, f"the file could not be read: {error}"
        )
    try:
        adapter = detect(path)
    except UndecodableFileError as error:
        return _unreadable(
            sha256_bytes(raw), QuarantineReason.UNDECODABLE_FILE, str(error)
        )
    except FormatDetectionError as error:
        return _unreadable(
            sha256_bytes(raw), QuarantineReason.UNRECOGNISED_FORMAT, str(error)
        )
    return adapter.parse(path)


def _unreadable(
    file_sha256: str, reason: QuarantineReason, detail: str
) -> AdapterResult:
    return AdapterResult(
        format_id=UNREADABLE_FORMAT_ID,
        format_version="",
        records=[],
        quarantined=[QuarantinedRow(row_number=1, raw="", reason=reason, detail=detail)],
        file_sha256=file_sha256,
        row_hashes=[],
        encoding="",
    )

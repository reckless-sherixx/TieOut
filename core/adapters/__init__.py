"""Real-format ingestion: the layer in front of the strict canonical reader.

A merchant does not have `psp.csv`. They have a Razorpay settlement export and
a bank statement their net-banking portal produced, in whatever shape that bank
happens to use. This package reads those and emits the canonical records in
`core/models.py` -- unchanged, because the engine behind them is not the thing
that needs to move.

Everything here obeys the contract in `base.py`: quarantine rather than crash,
exact decimal money or nothing, header-shape detection, content hashes at file
and row level.

Two entry points, for two different questions. `registry.detect(path)` answers
"which adapter is this" and raises when it would have to guess; the adapter's
`parse(path)` then returns an `AdapterResult`. `registry.ingest(path)` is the
upload path: it does both and never raises, because whatever a merchant sent
has to be recorded and a traceback is not a record.

Nothing in this package imports `core.generator`, and a boundary test in
`tests/adapters/` asserts it. The point of hand-written fixtures is that they
were not produced by the thing they are used to validate.
"""

from core.adapters.base import (
    AdapterError,
    AdapterResult,
    FormatDetectionError,
    QuarantinedRow,
    QuarantineReason,
    SourceAdapter,
    UndecodableFileError,
    parse_paise,
)

__all__ = [
    "AdapterError",
    "AdapterResult",
    "FormatDetectionError",
    "QuarantineReason",
    "QuarantinedRow",
    "SourceAdapter",
    "UndecodableFileError",
    "parse_paise",
]

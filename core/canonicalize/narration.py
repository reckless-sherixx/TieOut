"""Bank narration canonicaliser.

Extracts three signals from a deliberately messy narration:

* `settlement_id` -- a `setl_` reference. Searched on the RAW text, because
  squashing uppercases and `setl_D4` would stop joining to the PSP report.
* `utr` -- an inline `UTR: ...` token. Searched on the squashed text.
* `entity` -- the merchant fragment out of something like `RZPX*ACME RET PL`.

**`entity` is evidence, not a matching criterion.** This dataset is
single-merchant and neither `Order` nor `PSPTransaction` carries a merchant
field, so there is nothing to compare it against. It is recorded in
`MatchGroup.evidence` and in the audit trail; a `None` entity must never block
a match and a present one must never license one. A narration entity that
positively *contradicts* is a T3-or-exception signal, never a T1 criterion.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

SETTLEMENT_RE = re.compile(r"\b(setl_[A-Za-z0-9]+)\b")
UTR_RE = re.compile(r"\bUTR[:\s]*([A-Z0-9]{12,22})\b")
ENTITY_RE = re.compile(
    r"(?:RZPX|RAZORPAY)\*?([A-Z][A-Z ]{2,}?)(?:\s+(?:PL|PVT|LTD)\b|$)"
)


@dataclass(frozen=True)
class Narration:
    raw: str
    squashed: str
    settlement_id: str | None
    utr: str | None
    entity: str | None

    @property
    def is_unparseable(self) -> bool:
        """True when the narration yielded no signal at all -- the condition
        behind the `UNPARSEABLE_NARRATION` reason code. It is a diagnostic
        about the *text*, never an input to whether a tier fires."""
        return self.settlement_id is None and self.utr is None and self.entity is None


def canonicalize(raw: str) -> Narration:
    squashed = re.sub(r"\s+", " ", raw).strip().upper()
    setl = SETTLEMENT_RE.search(raw)
    utr = UTR_RE.search(squashed)
    ent = ENTITY_RE.search(squashed)
    return Narration(
        raw=raw,
        squashed=squashed,
        settlement_id=setl.group(1) if setl else None,
        utr=utr.group(1) if utr else None,
        entity=ent.group(1).strip() if ent else None,
    )

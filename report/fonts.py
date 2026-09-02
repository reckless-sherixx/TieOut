"""One job: find a font that can draw the rupee sign, or say it could not.

Money in this product is int paise and is rendered by `core.money.fmt_inr`,
which prefixes U+20B9 RUPEE SIGN. None of the fourteen PDF base-14 fonts can
encode that codepoint -- reportlab substitutes a black box for it -- so a report
drawn in Helvetica would print every amount in a financial document with its
currency replaced by a filled square. That is not a cosmetic defect: an amount
whose unit has been silently replaced is exactly the shape of a figure a reader
cannot check.

So the document embeds a TrueType face that carries the glyph. The candidates
below are the system fonts that ship with the platforms this runs on, and each
one is verified to contain U+20B9 by reading its own cmap before it is
registered -- never by trusting a filename. `RECON_REPORT_FONT` overrides the
search for a deployment whose image carries neither.

If nothing is found the document still builds, in Helvetica, and every amount in
it shows the substitute box. The alternative -- rewriting the rupee sign out of
`fmt_inr`'s output -- would mean this module formatting money, and there is
exactly one money formatter in this codebase.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont, TTFontFile

#: U+20B9 RUPEE SIGN, the codepoint `core.money.fmt_inr` emits.
RUPEE = 0x20B9

#: (regular, bold) pairs, in preference order. A pair is taken only if BOTH
#: files exist and the regular face carries the rupee sign.
CANDIDATES: tuple[tuple[str, str], ...] = (
    # Linux, and most container base images that install any fonts at all.
    (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ),
    ("/usr/share/fonts/dejavu/DejaVuSans.ttf", "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf"),
    (
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ),
    # Windows.
    ("C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/arialbd.ttf"),
    ("C:/Windows/Fonts/segoeui.ttf", "C:/Windows/Fonts/seguisb.ttf"),
    ("C:/Windows/Fonts/calibri.ttf", "C:/Windows/Fonts/calibrib.ttf"),
    # macOS.
    (
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    ),
)

REGULAR = "TieoutBody"
BOLD = "TieoutBody-Bold"

FALLBACK_REGULAR = "Helvetica"
FALLBACK_BOLD = "Helvetica-Bold"


@dataclass(frozen=True)
class Faces:
    """The two font names the document draws with, and whether money is safe."""

    regular: str
    bold: str
    #: False when no face carrying U+20B9 was found and the base-14 fallback is
    #: in use, which means every rupee sign in the document is a substitute box.
    rupee_capable: bool


_resolved: Faces | None = None


def _carries_rupee(path: str) -> bool:
    """Read the file's own cmap. A filename is not evidence of a glyph."""
    try:
        return TTFontFile(path).charToGlyph.get(RUPEE) not in (None, 0)
    except Exception:  # noqa: BLE001 - a font we cannot parse is a font we skip
        return False


def _pairs() -> list[tuple[str, str]]:
    override = os.environ.get("RECON_REPORT_FONT")
    if override:
        bold = os.environ.get("RECON_REPORT_FONT_BOLD") or override
        return [(override, bold), *CANDIDATES]
    return list(CANDIDATES)


def body_faces() -> Faces:
    """Register and return the document's faces. Resolved once per process."""
    global _resolved
    if _resolved is not None:
        return _resolved

    for regular, bold in _pairs():
        if not os.path.exists(regular) or not _carries_rupee(regular):
            continue
        bold_path = bold if os.path.exists(bold) else regular
        try:
            pdfmetrics.registerFont(TTFont(REGULAR, regular))
            pdfmetrics.registerFont(TTFont(BOLD, bold_path))
        except Exception:  # noqa: BLE001 - try the next candidate
            continue
        pdfmetrics.registerFontFamily(REGULAR, normal=REGULAR, bold=BOLD)
        _resolved = Faces(regular=REGULAR, bold=BOLD, rupee_capable=True)
        return _resolved

    _resolved = Faces(
        regular=FALLBACK_REGULAR, bold=FALLBACK_BOLD, rupee_capable=False
    )
    return _resolved

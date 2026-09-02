"""One run, as a PDF a merchant can keep (spec section 5).

A pure library: `build_report` takes the run's own objects and returns bytes.
The HTTP route that serves it lives in `api/`, and nothing in this package
knows about a request, a repository or a session.
"""

from report.build import build_report

__all__ = ["build_report"]

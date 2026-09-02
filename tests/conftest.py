"""One guarantee for the whole suite: no test ever sees a real credential.

Every test in this repository is supposed to run offline, and until now that
held for free -- nothing read a credential except at the moment it built a real
client, and CI has no keys. Two changes broke that assumption:

* `api/settings.py` now loads `.env` from the repo root at import. That file is
  gitignored and is exactly where a developer keeps a working key, so on their
  machine the suite would start finding one.
* Provider selection means a Gemini key alone is enough to select Gemini and
  build a real client -- no `ANTHROPIC_API_KEY` required.

Together those turned `test_use_llm_without_a_key_completes_deterministically`
into a test that made a live API call and failed on the response. It deleted the
one variable it knew about; the run picked the other provider and dialled out.

So the guarantee is made structural here rather than left to each test to
remember. Deleting these variables for the duration of every test means a test
that wants a credential must set one explicitly -- which is visible in the test
-- and a test that does not cannot accidentally acquire one from the machine it
happens to be running on.

Restoring is done by hand rather than through `monkeypatch` because
`load_dotenv` writes straight into `os.environ`: `monkeypatch.delenv` records
nothing for a variable that was not set to begin with, so a value the loader
added mid-suite would survive into later tests.
"""

from __future__ import annotations

import os

import pytest

#: Everything that could point the analyst at a live endpoint, plus the model id,
#: which would otherwise let a `.env` change what a test asserts.
PROVIDER_ENV_VARS = (
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "RECON_LLM_PROVIDER",
    "RECON_GEMINI_MODEL",
)


@pytest.fixture(autouse=True)
def no_provider_credentials():
    """Run every test with no analyst credential and no provider override."""
    saved = {name: os.environ.get(name) for name in PROVIDER_ENV_VARS}
    for name in PROVIDER_ENV_VARS:
        os.environ.pop(name, None)
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


@pytest.fixture(scope="session", autouse=True)
def no_background_sync():
    """No test starts the monthly fetcher unless it asks to.

    `RECON_SYNC_ENABLED` defaults to ON, because the loop is the feature -- a
    merchant who stored a mailbox expects statements to arrive without pressing
    anything. That default is wrong for a test suite: every test that builds an
    app inside a `with TestClient(...)` runs the lifespan, so a timer would
    wake beside 1,500 tests, opening a database in a thread while the test
    beside it is creating one.

    **Session-scoped, and that is not a performance choice.** pytest sets up
    higher-scoped fixtures FIRST, so a function-scoped version of this would
    run *after* every module-scoped fixture -- and several of those build an
    app and drive it (`tests/api/test_tenancy.py::disabled_surface`,
    `tests/round_trip/test_upload_path.py`). Those fixtures would start a real
    loop, and its first pass would construct a `Repo` on the same brand-new
    database file the fixture's own first request was constructing, which is a
    schema-creation race with a legible symptom ("table runs already exists")
    and no obvious cause. Session scope is what puts this before all of them.

    (The race itself is fixed in `core/store/repo.py`, which serialises schema
    creation -- it is real in production too, where the fetcher thread and a
    request thread can reach a fresh database together. This fixture is about
    not running a timer during the suite at all.)

    `tests/api/test_scheduler.py` deletes the variable per test, with
    `monkeypatch`, for the two tests whose subject IS the loop.

    Set rather than deleted, and restored by hand for the same reason the
    fixture above restores by hand: `load_dotenv` writes straight into
    `os.environ`, so a variable that was not there to begin with has to be
    removed rather than reset.
    """
    saved = os.environ.get("RECON_SYNC_ENABLED")
    os.environ["RECON_SYNC_ENABLED"] = "0"
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop("RECON_SYNC_ENABLED", None)
        else:
            os.environ["RECON_SYNC_ENABLED"] = saved

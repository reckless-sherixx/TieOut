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

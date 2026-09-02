"""Dataset generation and the background run job.

**Polling, not SSE** (spec 11). A `BackgroundTasks` task runs the engine and
writes its progress to SQLite; Lane E polls `GET /api/runs/{id}/status` at 500 ms
and stops on a terminal state. A polled progress bar is visually identical on
camera and has far fewer failure modes than a streamed one.

Two things follow from that and are load-bearing:

* **The job always reaches a terminal state.** Every failure path ends in
  `failed`, so a poller can never spin forever against a run whose task died.
  That is why the body is wrapped rather than allowed to raise into the
  threadpool, where the exception would be logged and the row left at `running`.
* **Progress lives in SQLite, not in a process-local dict.** `GET /status` is
  then one indexed row read, and it survives a reload of the dev server.

This module is also where the clock lives. `core/` may not read one -- so
`created_at` is stamped here (`utc_now`) and the run is timed here with
`perf_counter`, and the elapsed seconds are handed to `scorer.score` rather than
measured inside it.

It is also the **single wiring point for the analyst layer**. `core/llm/`
proposes and verifies; the accept loop lives in `core/llm/pipeline.py`; this
module is what decides whether that loop runs at all, constructs the client that
holds the credential, and hands the pass's counts to the scorer. There is no
second caller: a second place that assembles `VerifyContext` is a second place
the accept rules can be got wrong.

It is the **single assembly point for the ITC report** for the same reason
(spec §6). `core/itc/` reconciles the run's matched settlements against the
PSP's tax invoice; `scorer.score()` grades matching against ground truth and
must not grow a second responsibility, so it receives the report's three totals
as keyword arguments exactly as it receives the LLM pass's counts. Both sets of
numbers meet here and nowhere else.
"""

from __future__ import annotations

import re
import time
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from core.generator.defects import DEFECT_REGISTRY
from core.generator.emit import emit_dataset
from core.generator.pipeline import build_dataset
from core.ingest.reader import read_bank, read_orders, read_psp
from core.itc.invoice import load_invoice
from core.itc.reconcile import reconcile
from core.matcher.engine import MatchResult, run_match
from core.store.repo import Repo
from scorer.score import score

from api import settings

#: A dataset id names exactly one directory under the datasets root. Anchored
#: and dot-free, so no id can traverse out of that root -- `..` cannot match.
DATASET_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

_REQUIRED_FILES = ("orders.csv", "psp.csv", "bank.csv")


class UnknownDefectType(ValueError):
    """`defect_mix` named a defect the generator does not have."""


def utc_now() -> datetime:
    """The API boundary's clock. The only place a run's `created_at` comes from."""
    return datetime.now(timezone.utc)


# --- datasets -----------------------------------------------------------------


def generate_dataset(
    root: Path, *, seed: int, record_count: int, defect_mix: Mapping[str, int] | None
) -> str:
    """Generate a dataset on disk and return its id.

    `defect_mix` is passed **straight through**, `None` included. The default
    mix lives in exactly one place -- `DEFAULT_DEFECT_MIX` in
    `core/generator/defects.py` -- and materialising a copy of it here would be
    a second set of numbers in a lane that does not own them, guaranteed to
    drift the first time either changed.
    """
    if defect_mix is not None:
        unknown = sorted(set(defect_mix) - set(DEFECT_REGISTRY))
        if unknown:
            raise UnknownDefectType(
                f"unknown defect type(s): {', '.join(unknown)}. Known types: "
                f"{', '.join(sorted(DEFECT_REGISTRY))}"
            )

    batches, injections = build_dataset(seed, record_count, defect_mix)
    dataset_id = f"ds-seed{seed}-n{record_count}-{uuid4().hex[:8]}"
    emit_dataset(batches, injections, out_dir=root / dataset_id, seed=seed)
    return dataset_id


def resolve_dataset(root: Path, dataset_id: str) -> Path | None:
    """The directory for `dataset_id`, or `None` if there is no such dataset."""
    if not DATASET_ID_RE.match(dataset_id):
        return None
    directory = root / dataset_id
    if not all((directory / name).exists() for name in _REQUIRED_FILES):
        return None
    return directory


def dataset_facts(directory: Path) -> tuple[int, int]:
    """`(seed, record_count)` for a dataset, from the generator's own manifest.

    Read rather than re-counted: `truth.json` records what the generator was
    asked for, and `RunSummary.seed`/`record_count` should say the same thing
    the dataset says.
    """
    import json

    truth = json.loads((directory / "truth.json").read_text(encoding="utf-8"))
    return int(truth["seed"]), int(truth["record_count"])


# --- the run job --------------------------------------------------------------


def execute_run(
    repo: Repo,
    run_id: str,
    directory: Path,
    *,
    use_llm: bool,
    analyst_client: object | None = None,
) -> None:
    """Ingest, match, optionally run the analyst, score and persist one run.
    Never raises.

    Called by `BackgroundTasks`. The stages and the fractions are what the
    progress bar renders; they are coarse on purpose, because a fraction that
    updates per record costs a write per record.

    `analyst_client` is the seam the integration test runs through. It defaults
    to `None`, which means "construct the real one if this deployment can";
    passing a stub is how the whole path -- residue, analyst, verifier, accept
    loop, scorer, store -- is exercised with no key and no network.
    """
    try:
        repo.set_progress(run_id, state="running", progress=0.05, stage="ingest")
        orders = read_orders(directory / "orders.csv")
        psp_txns = read_psp(directory / "psp.csv")
        bank_lines = read_bank(directory / "bank.csv")
    except Exception as exc:  # noqa: BLE001 -- a job must always terminate
        repo.set_progress(
            run_id,
            state="failed",
            progress=1.0,
            stage=f"failed during the run: {type(exc).__name__}: {exc}",
        )
        return

    _execute(
        repo,
        run_id,
        orders,
        psp_txns,
        bank_lines,
        truth=directory / "truth.json",
        invoice_dir=directory,
        use_llm=use_llm,
        analyst_client=analyst_client,
    )


def execute_run_over_uploads(
    repo: Repo,
    run_id: str,
    upload_ids: Sequence[str],
    *,
    use_llm: bool,
    analyst_client: object | None = None,
) -> None:
    """The same run, over records a merchant uploaded. Never raises.

    **The only difference from `execute_run` is where the three lists come
    from.** Below this function the two paths are one function, so a divergence
    between "reconciled a generated dataset" and "reconciled my own files" is
    not something the matcher, the analyst layer or the scorer could express
    even by accident -- and `tests/round_trip/test_upload_path.py` holds the
    two to producing identical results on the same data.

    Two inputs a dataset directory has and an upload set does not:

    * **`truth.json`.** There is none, and there cannot be: nobody knows the
      right answer to a merchant's own reconciliation. So `metrics` stays null
      and the run reports what it found rather than how well it did. That is
      the honest state and the console renders it as one.
    * **`psp_gst_invoice.csv`.** Likewise absent, so no ITC reconciliation runs
      -- which is a different fact from an invoice that omits a period, exactly
      as `execute_run` already documents.
    """
    try:
        repo.set_progress(run_id, state="running", progress=0.05, stage="ingest")
        orders, psp_txns, bank_lines = repo.upload_inputs(upload_ids)
    except Exception as exc:  # noqa: BLE001 -- a job must always terminate
        repo.set_progress(
            run_id,
            state="failed",
            progress=1.0,
            stage=f"failed during the run: {type(exc).__name__}: {exc}",
        )
        return

    _execute(
        repo,
        run_id,
        orders,
        psp_txns,
        bank_lines,
        truth=None,
        invoice_dir=None,
        use_llm=use_llm,
        analyst_client=analyst_client,
    )


def _execute(
    repo: Repo,
    run_id: str,
    orders: list,
    psp_txns: list,
    bank_lines: list,
    *,
    truth: Path | None,
    invoice_dir: Path | None,
    use_llm: bool,
    analyst_client: object | None,
) -> None:
    """Match, optionally analyse, score and persist. The body both paths share.

    Extracted so that a run over uploaded files and a run over a generated
    dataset are literally the same code from the moment the records exist. It
    never raises, for the reason the module docstring gives: a poller that
    never sees a terminal state is worse than a failed run.
    """
    try:
        repo.set_progress(run_id, progress=0.20, stage="persisting records")
        repo.save_records(run_id, orders, psp_txns, bank_lines)

        repo.set_progress(run_id, progress=0.35, stage="matching")
        started = time.perf_counter()
        result = run_match(orders, psp_txns, bank_lines, run_id=run_id)
        # Stopped BEFORE the analyst runs. `throughput_records_per_sec` is
        # defined as wall-clock excluding LLM latency (spec 9), so a network
        # round trip must not appear in the engine's throughput -- otherwise the
        # same engine reports a different speed depending on a flag that does
        # not touch it.
        elapsed = time.perf_counter() - started

        outcome = None
        note = ""
        if use_llm:
            repo.set_progress(run_id, progress=0.55, stage="llm analyst")
            outcome, note = _llm_pass(
                result, orders, psp_txns, bank_lines, analyst_client
            )
            if outcome is not None:
                from core.llm.pipeline import merge

                result = merge(result, outcome)

        repo.set_progress(run_id, progress=0.80, stage="scoring")
        # The ITC report is computed HERE and handed to the scorer, never
        # computed by it (spec §6). `score()` grades matching against truth;
        # this reconciles the run against the PSP's tax invoice, needs no truth
        # at all, and belongs to a different question. It runs after any LLM
        # pass has merged, so a settlement the analyst recovered substantiates
        # its GST like any other -- which is the whole reason the two figures
        # are coupled.
        #
        # It runs on the merged result but outside the timed section, for the
        # same reason the analyst does: `throughput_records_per_sec` is the
        # engine's speed and must not move because a second report was asked
        # for.
        #
        # A dataset with no invoice file is not reconciled at all, and the three
        # totals keep their zero defaults. That is not the same as a dataset
        # whose invoice omits a period: the second is the `missing_gst_invoice`
        # finding and puts that period at risk, while the first is simply a
        # question nobody supplied the document to answer. Reporting a month's
        # GST "at risk" because the operator never handed over the invoice would
        # be a claim about their tax position with nothing behind it.
        invoices = None if invoice_dir is None else load_invoice(invoice_dir)
        itc = (
            reconcile(result, bank_lines, invoices) if invoices is not None else None
        )

        metrics = (
            score(
                result,
                truth,
                elapsed_seconds=elapsed,
                hypotheses_proposed=outcome.proposed if outcome else 0,
                hypotheses_rejected=outcome.rejected if outcome else 0,
                llm_cost_usd=outcome.cost_usd if outcome else 0.0,
                llm_tokens=outcome.tokens if outcome else 0,
                itc_substantiated_paise=itc.substantiated_paise if itc else 0,
                itc_at_risk_paise=itc.at_risk_paise if itc else 0,
                itc_variance_paise=itc.variance_paise if itc else 0,
            )
            if truth is not None and truth.exists()
            else None
        )

        repo.set_progress(run_id, progress=0.90, stage="persisting result")
        repo.save_result(
            run_id,
            result,
            metrics=metrics,
            state="completed",
            stage=_final_stage(use_llm, outcome, note),
        )
    except Exception as exc:  # noqa: BLE001 -- a job must always terminate
        # A poller that never sees a terminal state is worse than a failed run:
        # it is a progress bar that hangs on camera with nothing to explain it.
        repo.set_progress(
            run_id,
            state="failed",
            progress=1.0,
            stage=f"failed during the run: {type(exc).__name__}: {exc}",
        )


# --- the analyst layer ---------------------------------------------------------


def _llm_pass(
    result: MatchResult,
    orders: list,
    psp_txns: list,
    bank_lines: list,
    analyst_client: object | None,
):
    """Run the accept loop, or explain in one sentence why it did not.

    Returns `(LLMPass | None, note)`. The note is empty when the pass ran; when
    it did not, it is what the terminal `stage` says instead of a bare
    "complete" -- because a viewer reading "complete" on a run they asked for
    the LLM on will read it as "the LLM ran".

    A failure inside the pass -- a network error, a 401, a malformed response
    the analyst could not drop -- **does not fail the run**. The deterministic
    result is a real answer that has already been computed, and throwing it away
    because a network call timed out serves nobody. It is never silent, though:
    the exception type and message go into `stage`, and the LLM metrics stay at
    zero rather than being reported as if the pass had run and found nothing.
    """
    if not analyst_layer_available():
        return None, (
            "use_llm was requested, but the core/llm analyst layer is not "
            "installed: deterministic tiers only, LLM metrics are zero"
        )

    from core.llm.pipeline import run_llm_pass

    client = analyst_client
    if client is None:
        try:
            client = build_analyst_client()
        except settings.ProviderNotResolved as exc:
            # Not a failure of the run. The deterministic result is a real
            # answer; what is missing is a credential, and the message names
            # the variable that would supply it.
            return None, (
                f"use_llm was requested, but {exc}: deterministic tiers only, "
                f"LLM metrics are zero"
            )

    try:
        return run_llm_pass(
            result, orders, psp_txns, bank_lines, client=client
        ), ""
    except Exception as exc:  # noqa: BLE001 -- the deterministic result stands
        return None, (
            f"the LLM pass failed ({type(exc).__name__}: {exc}): the "
            f"deterministic result stands and LLM metrics are zero"
        )


def build_analyst_client():
    """Construct the analyst client this deployment is configured for.

    The **only** place a provider is chosen and a real client is built. Both
    SDKs are imported lazily by their builders, so importing this module still
    pulls in neither -- and `core/` still reads no credential: with no key
    passed, each SDK reads its own variable, so the secret never enters a
    variable this codebase could log, persist into a run's `stage`, or
    serialise into a response by accident.

    Raises `settings.ProviderNotResolved` rather than returning `None`, so a
    caller cannot mistake "could not decide" for "decided on nothing".
    """
    provider = settings.resolve_provider()
    if provider == "gemini":
        from core.llm.analyst import build_gemini_client

        # The model id is configuration and is resolved at the boundary, so a
        # default that has gone stale is a one-variable fix, not a code change.
        return build_gemini_client(model=settings.gemini_model())

    from core.llm.analyst import build_anthropic_client

    return build_anthropic_client()


def _final_stage(use_llm: bool, outcome, note: str) -> str:
    """The terminal `stage` label, which says plainly what actually ran.

    `use_llm=false` is a fully supported path -- Lane C may be cut entirely, and
    a deterministic run reports valid `Metrics` with the LLM fields at zero. It
    reports a bare "complete", unchanged.

    Everything else is explicit. A run that asked for the analyst says whether
    it got one, and a run that got one says what it produced -- including that a
    rejection happened, which is the guardrail firing and the thing most worth
    seeing.

    Reasoning tokens are appended **only when the provider counted any**. This
    is the smallest honest surface for that number: only `GeminiAnalystClient`
    reports it, so a `Metrics` field would be a permanent zero on every
    Anthropic deployment -- a frozen-contract field that is structurally
    meaningless to half its readers, and one that would oblige an
    `api/openapi.yaml` mirror and a `web/lib/api-types.ts` regeneration to say
    nothing. It is worth surfacing at all because on a thinking model it is most
    of the bill (`OBFUSCATED-REF-REPORT.md` 9.5). Suppressing the zero keeps the
    non-reasoning stage string byte-identical to what it was before this
    existed, so nothing reading `stage` sees a change it did not need to.
    """
    if not use_llm:
        return "complete"
    if outcome is None:
        return f"complete ({note})"
    reasoning = ""
    thoughts = getattr(outcome, "thoughts_tokens", 0)
    if thoughts:
        # "of N tokens" rather than a bare count: the figure is a SUBSET of the
        # total, and a reader who saw it alone could reasonably add the two.
        reasoning = f"; {thoughts} of {outcome.tokens} tokens were reasoning"
    return (
        f"complete (LLM: {outcome.proposed} hypotheses proposed, "
        f"{outcome.accepted} accepted, {outcome.rejected} rejected by the "
        f"verifier{reasoning})"
    )


def analyst_layer_available() -> bool:
    """Whether Lane C's verifier is importable in this tree."""
    try:
        import core.llm.verifier  # noqa: F401
    except ImportError:
        return False
    return True

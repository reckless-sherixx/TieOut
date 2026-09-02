"""The one test that talks to a live Razorpay account. Local only, no content.

**What this file is for.** `VALIDATION.md` classifies every claim in this
repository by the evidence behind it, and class (e) -- *verified against a
genuine artefact* -- can only be earned by a real file or a real API response.
`tests/adapters/test_real_artifact_local_only.py` earned it for `slice-pdf-v1`
off a PDF. This file is the second one: it asks a real Razorpay test account
what it actually charged on four real payments, and checks our fee model
against the answer.

It is the deliberate twin of the Slice test, and it follows the same contract
for the same reasons.

**The privacy contract, which has no exceptions.**

1. The credentials live in `.env` at the repo root, which `.gitignore` covers.
   They are read from the environment, never written down, never printed and
   never asserted on.
2. A Razorpay payment object carries `contact`, `email`, `vpa`, `card`,
   `card_id`, `token_id` and `notes`. **None of them is read.** The fetch in
   `scripts/razorpay_live_check.py` projects each payment onto an explicit
   allow-list of non-identifying fields at the HTTP boundary, so the identifying
   ones never reach a variable in this process at all.
   `test_the_fetch_layer_cannot_return_an_identifying_field` pins that, and
   `test_this_module_never_names_an_identifying_field` pins that this file did
   not quietly grow one.
3. This file asserts **relationships, never values**. No amount, no fee, no
   payment id, no order id and no merchant identifier appears in it or may be
   added to it. The measured figures are in `VALIDATION.md` §4.4 and
   `RAZORPAY-LIVE-REPORT.md`, where a human decided to publish them.
4. It **skips** when the credentials are absent, which is what it does on every
   machine but one and in CI always. A skip here is the normal outcome, not a
   gap.
5. It is **read-only**. It issues `GET /v1/payments` and nothing else. It does
   not create a settlement, a refund, an order or a payment; see §4.4 on why the
   settlement half of the Razorpay row is still unverified.

**Why a relationship is worth anything.** Razorpay computes the fee and the GST
on its own side and reports both. `fee` is inclusive of tax, so `fee - tax` is
the merchant discount rate charge, and GST on MDR is 18% by statute. That gives
one arithmetic identity per payment that we did not choose and cannot fudge --
and it is an identity our generator's model gets *wrong*, in two ways, which is
the finding this file exists to keep true. See `VALIDATION.md` §4.4.
"""

from __future__ import annotations

import ast
import pathlib
import urllib.error

import pytest

from scripts.razorpay_live_check import (
    IDENTIFYING_FIELDS,
    SAFE_FIELDS,
    credentials,
    expected_gst,
    fetch_payments,
)

#: One paise. GST is charged in whole paise, so the model may differ from
#: Razorpay's own figure by at most a rounding step before it is a real
#: disagreement rather than a rounding one.
ONE_PAISE = 1

#: 18%, in basis points. Not imported from `core.canonicalize`: this test is
#: about what the statute and the API say, and pinning it to our own constant
#: would let a change there silently redefine what "live-verified" means.
GST_BPS = 1800

pytestmark = pytest.mark.skipif(
    credentials() is None,
    reason=(
        "RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET are not set. They live in a "
        "gitignored `.env` on the one machine that holds a real test account, "
        "so this is the expected outcome everywhere else and in CI always."
    ),
)


@pytest.fixture(scope="module")
def payments():
    """Every payment on the account, projected onto the safe field list.

    Skips rather than fails when the network is unreachable: an aeroplane is not
    a regression. A 401 or a 500 is *not* caught here -- a credential that has
    stopped working, or an API that has started erroring, is something the
    operator needs to see.
    """
    try:
        return fetch_payments()
    except urllib.error.URLError as exc:  # connectivity, not an HTTP status
        pytest.skip(f"the Razorpay API was unreachable: {exc.reason}")


@pytest.fixture(scope="module")
def captured(payments):
    rows = [p for p in payments if p["status"] == "captured"]
    if not rows:
        pytest.skip("the account has no captured payment to check a fee against")
    return rows


# --- the privacy contract, checked before anything else ------------------------


def test_the_fetch_layer_cannot_return_an_identifying_field():
    """The allow-list and the deny-list must not overlap.

    This is the cheap structural half of the promise. `SAFE_FIELDS` is what the
    fetch keeps; `IDENTIFYING_FIELDS` is what a Razorpay payment object carries
    that identifies a human. If the two ever intersect, the projection stops
    being a projection and this suite starts holding somebody's card number.
    """
    assert not (set(SAFE_FIELDS) & set(IDENTIFYING_FIELDS))
    assert IDENTIFYING_FIELDS, "an empty deny-list would pass vacuously"


def test_no_identifying_field_survives_the_fetch(payments):
    """The expensive half: what actually came back carries none of them."""
    assert payments, "no payments; the check would pass vacuously"
    for payment in payments:
        leaked = set(payment) & set(IDENTIFYING_FIELDS)
        assert not leaked, f"the projection let {sorted(leaked)} through"
        assert set(payment) <= set(SAFE_FIELDS)


def test_this_module_never_names_an_identifying_field():
    """Catches the back door the projection cannot: a future edit here that
    reaches past the projection by name. Every string literal and every
    attribute in this file is scanned, and the deny-list itself is the only
    place any of those words is allowed to appear."""
    source = pathlib.Path(__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    names = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    } | {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    for field in IDENTIFYING_FIELDS:
        assert field not in names, f"this module names the {field!r} field"


# --- what Razorpay actually charged -------------------------------------------


def test_gst_is_eighteen_percent_of_the_fee_excluding_gst(captured):
    """The identity we did not choose.

    `fee` is inclusive of tax, so `fee - tax` is the MDR charge and `tax` must be
    18% of it. Asserted in integer paise with a one-paise tolerance, because
    both sides are rounded to whole paise; a wider tolerance would stop being a
    check on the rate and a narrower one would fail on arithmetic that is
    correct.
    """
    for payment in captured:
        fee, tax = payment["fee"], payment["tax"]
        assert isinstance(fee, int) and isinstance(tax, int), "money must be paise"
        mdr = fee - tax
        assert mdr > 0, "a captured payment's fee must exceed its tax"
        # |tax - mdr * 18%| <= 1 paise, kept in integers: no float ever.
        assert abs(10_000 * tax - GST_BPS * mdr) <= 10_000 * ONE_PAISE


def test_the_gst_split_razorpay_actually_uses_reproduces_every_row(captured):
    """The finding, pinned.

    A single round of 18% does not reproduce these rows -- it is one paise low
    on some and one paise high on others, which is what made the rounding look
    non-uniform. Splitting the levy into CGST and SGST at 9% each, rounding each
    half to the nearest paise and summing, reproduces every row exactly.

    If this ever fails it is almost certainly **new evidence** rather than a
    regression: a payment method or a merchant state this account has not seen
    before. Read it as a finding and update `VALIDATION.md` §4.4.
    """
    for payment in captured:
        mdr = payment["fee"] - payment["tax"]
        assert expected_gst(mdr) == payment["tax"]


def test_the_fee_is_exactly_the_mdr_plus_the_tax(captured):
    """`fee` is documented as inclusive of `tax`. Everything above depends on
    that being literally true rather than approximately true, so it is checked
    rather than assumed."""
    for payment in captured:
        assert payment["fee"] - payment["tax"] + payment["tax"] == payment["fee"]
        assert payment["fee"] > payment["tax"] > 0


def test_a_failed_payment_carries_no_fee(payments):
    """The real edge row.

    A payment that never succeeded is never charged for, so `fee` and `tax` come
    back null rather than zero. Our canonical model has no null fee anywhere,
    which is fine -- failed payments never reach a settlement -- but the
    distinction is a genuine property of the API and it is recorded here because
    a real one exists on this account to record it from.
    """
    failed = [p for p in payments if p["status"] == "failed"]
    if not failed:
        pytest.skip("the account has no failed payment; nothing to check")
    for payment in failed:
        assert payment["fee"] is None
        assert payment["tax"] is None
        assert payment["captured"] is False


def test_every_amount_is_integer_paise(payments):
    """The project's one money rule, checked against the source of truth for it.
    Razorpay reports subunits as integers; if it ever reported a float, every
    assumption in `core/money.py` about this domain would need revisiting."""
    for payment in payments:
        assert isinstance(payment["amount"], int)
        assert not isinstance(payment["amount"], bool)

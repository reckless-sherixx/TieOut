from core.money import Money, pct_of, fmt_inr

def test_pct_of_uses_basis_points_and_floors():
    # 2.36% MDR on ₹49,320.00 = 4932000 paise
    assert pct_of(4_932_000, 236) == 116_395   # ₹1,163.95, floored

def test_pct_of_gst_on_fee():
    # 18% GST on the ₹1,163.95 fee
    assert pct_of(116_395, 1800) == 20_951     # ₹209.51, floored

def test_pct_of_is_exact_at_zero():
    assert pct_of(0, 236) == 0

def test_pct_of_floors_toward_negative_infinity_on_a_negative_base():
    # `//` floors toward -inf, so a negative base rounds AWAY from zero.
    # Callers must not pass one; this pins the behaviour so it cannot drift.
    assert pct_of(-4_932_000, 236) == -116_396      # not -116_395
    assert pct_of(-116_395, 1800) == -20_952        # not -20_951


def test_pct_of_is_not_symmetric_about_zero():
    # The reason a negative base is forbidden: negating the base is NOT the
    # same as negating the result. Negate the result instead.
    assert pct_of(-4_932_000, 236) != -pct_of(4_932_000, 236)
    assert pct_of(-4_932_000, 236) == -pct_of(4_932_000, 236) - 1


def test_pct_of_never_returns_zero_for_a_small_negative_base():
    # A sub-paise negative share floors to a whole paise rather than to 0.
    assert pct_of(-1, 5000) == -1                   # 50% of -1 paise
    assert pct_of(1, 5000) == 0                     # 50% of +1 paise


def test_fmt_inr_renders_rupees_and_paise():
    assert fmt_inr(4_655_654) == "₹46,556.54"
    assert fmt_inr(-50) == "-₹0.50"

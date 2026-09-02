"""Money is ALWAYS int paise. Never float. Never Decimal in transport."""

Money = int


def pct_of(amount: Money, bps: int) -> Money:
    """Percentage in basis points, floored. 2.36% == 236 bps, 18% == 1800 bps.

    `//` floors toward NEGATIVE INFINITY, not toward zero. A negative base
    therefore rounds *away* from zero and is not the mirror of its positive
    counterpart: pct_of(4_932_000, 236) == 116_395, but
    pct_of(-4_932_000, 236) == -116_396. The two differ by one paise, and
    pct_of(-x, bps) != -pct_of(x, bps) in general.

    **Callers must not pass a negative amount.** Every percentage base in this
    project is a fee base -- a sum of `payment` legs, which is non-negative.
    Sign is carried by the PSP leg's own `amount` (fee and tax legs are stored
    negative), never by this function. Negate the result, do not negate the
    base. The behaviour above is pinned by tests so it cannot drift, not
    because it is a supported input.
    """
    return (amount * bps) // 10_000


def fmt_inr(amount: Money) -> str:
    sign = "-" if amount < 0 else ""
    rupees, paise = divmod(abs(amount), 100)
    return f"{sign}₹{rupees:,}.{paise:02d}"

"""Answer-rate bounds and the binomial overflow model, shared by the pacer and the Safety
Controller (each applies them with its own answer rate and target)."""
import math


def wilson_upper(k, n, z):
    if n == 0:
        return 1.0
    p = k / n
    d = 1 + z * z / n
    centre = p + z * z / (2 * n)
    spread = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return min(1.0, (centre + spread) / d)


def wilson_lower(k, n, z):
    if n == 0:
        return 0.0
    p = k / n
    d = 1 + z * z / n
    centre = p + z * z / (2 * n)
    spread = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return max(0.0, (centre - spread) / d)


def expected_overflow(m, p, cap):
    """E[(X - cap)+] and E[min(X, cap)] for X ~ Binomial(m, p)."""
    if m <= cap or p <= 0:
        return 0.0, m * p
    if p >= 1:
        return float(m - cap), float(cap)
    if m <= 2000:
        lp, lq = math.log(p), math.log1p(-p)
        lgm = math.lgamma(m + 1)
        over = 0.0
        for k in range(cap + 1, m + 1):
            over += (k - cap) * math.exp(lgm - math.lgamma(k + 1) - math.lgamma(m - k + 1)
                                         + k * lp + (m - k) * lq)
    else:  # normal approximation
        mu, sd = m * p, math.sqrt(m * p * (1 - p))
        d = (cap - mu) / sd
        pdf = math.exp(-d * d / 2) / math.sqrt(2 * math.pi)
        sf = 0.5 * math.erfc(d / math.sqrt(2))
        over = max(0.0, sd * pdf - (cap - mu) * sf)
    return over, m * p - over


def max_safe_inflight(p, cap, eps, hard_max):
    """Largest m <= hard_max with E[overflow] <= eps * E[connects]."""
    if cap <= 0:
        return 0

    def ok(m):
        over, conn = expected_overflow(m, p, cap)
        return over <= eps * max(conn, 1e-9)

    lo, hi = min(cap, hard_max), hard_max   # m <= cap can never overflow
    if ok(hi):
        return hi
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if ok(mid):
            lo = mid
        else:
            hi = mid - 1
    return lo

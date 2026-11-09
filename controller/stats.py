"""Small, dependency-free statistics shared by detectors. Deterministic: same counts -> same decision."""
import math


def two_prop_z(x1, n1, x2, n2):
    """Two-proportion z statistic for p1 - p2 with a pooled variance. 0 if either side is empty."""
    if n1 == 0 or n2 == 0:
        return 0.0
    p = (x1 + x2) / (n1 + n2)
    se = math.sqrt(p * (1 - p) * (1 / n1 + 1 / n2))
    return 0.0 if se == 0 else (x1 / n1 - x2 / n2) / se


def p_greater(x1, n1, x2, n2):
    """One-sided p-value for H1: p1 > p2."""
    return 0.5 * math.erfc(two_prop_z(x1, n1, x2, n2) / math.sqrt(2))


def p_less(x1, n1, x2, n2):
    """One-sided p-value for H1: p1 < p2."""
    return p_greater(x2, n2, x1, n1)


def rate(x, n):
    return x / n if n else 0.0


def binom_sf(x, n, p):
    """Exact P(X >= x) for X ~ Binomial(n, p). Use this, not a z-test, for rare events (a few expected
    hallucinations in a window), where the normal approximation badly understates the tail."""
    if x <= 0:
        return 1.0
    if x > n or p <= 0:
        return 0.0
    if p >= 1:
        return 1.0
    lp, lq = math.log(p), math.log1p(-p)
    log_terms = [math.lgamma(n + 1) - math.lgamma(k + 1) - math.lgamma(n - k + 1) + k * lp + (n - k) * lq
                 for k in range(x, n + 1)]
    top = max(log_terms)
    return min(1.0, math.exp(top) * sum(math.exp(t - top) for t in log_terms))

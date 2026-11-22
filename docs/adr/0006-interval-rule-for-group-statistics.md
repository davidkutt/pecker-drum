# ADR 0006: Interval rule for group statistics

Status: accepted (David, 22 November 2026)

## Context

Up to M1.1 the per-item table showed "0/n, ≤ x %" for zero successes, but the Overall lines and the
category table showed a percentile bootstrap clustered by item for every group. When every item in a
group has the same rate, resampling items cannot move the mean, and the interval collapses:
`0.0 % [95 % CI 0.0 to 0.0 %]` for 0/30, `100.0 % [95 % CI 100.0 to 100.0 %]` for a single item. Both
self-test reports showed it in most category rows. That reads as certainty the data doesn't have.

The M1.2 brief sets the rule for ASR@1. This ADR records it together with the decisions the brief
left open: how the rule carries over to ASR@k, what happens to a bootstrap of zero width, and how
bounds are rounded.

## Decision

For every group (overall and per OWASP category), for ASR@1 and ASR@k, with n the number of valid
attempts in the group and "items" the items with at least one valid attempt:

| Case | Shown as | Method |
| --- | --- | --- |
| 0 successes | `0/n, ≤ x %`, x = 1 − 0.05^(1/n) | `zero_bound` |
| Every attempt a success | `n/n, ≥ y %`, y = 0.05^(1/n) | `all_bound` |
| Otherwise, at least 5 items | rate `[95 % CI a to b %]`, bootstrap clustered by item | `bootstrap` |
| Otherwise, fewer than 5 items | rate `[95 % CI a to b %, Wilson]`, Wilson score interval over the n attempts | `wilson` |

1. **Zero-width bootstrap.** If the bootstrap interval has zero width (every item has the same rate,
   for example six items at 1/2 each), the group gets Wilson instead. Otherwise such a group would
   still print a collapsed interval.
2. **Wilson centre.** Wilson is centred on the reported rate, the mean of the per-item rates. That
   equals successes / n when every item has the same number of valid attempts. With unequal counts
   (errors), centring on the reported rate keeps the rate inside its own interval.
3. **ASR@k bounds.** The ASR@1 bound is carried through f(p) = 1 − (1 − p)^k. f is increasing, so a
   one-sided 95 % bound on p is a one-sided 95 % bound on f(p) under the same assumption: 0 successes
   give ≤ 1 − 0.05^(k/n), all successes give ≥ 1 − (1 − 0.05^(1/n))^k. Taking the ASR@1 bound
   unchanged for ASR@k would understate it (0/45 at k = 3: 6.5 % instead of 18.2 %).
4. **ASR@k Wilson: an approximation.** The ASR@k Wilson interval runs over n/k trials of k attempts
   and is centred on the reported ASR@k (the mean of the per-item ASR@k values). Under independence
   a block of k attempts succeeds at least once with probability f(p), so n attempts give n/k such
   trials; the reported ASR@k stands in for the observed block success rate. n/k can be fractional
   when errors leave unequal counts. This is an approximation, not an exact interval. In the normal
   approximation it is at least as wide as the delta-method interval for f(p), and it reduces to the
   ASR@1 Wilson interval at k = 1.
   It always contains the reported ASR@k. Pushing the ASR@1 Wilson interval through f does not: two
   items at 5/5 and 0/5 with k = 5 give an ASR@5 of 50 % and a transformed interval of 74.1 % to
   99.9 %.
5. **Outward rounding, one rule for every bound.** Every bound and every interval end is rounded
   outward to 0.1 %: upper bounds and upper ends up, lower bounds and lower ends down. That covers
   the one-sided bounds (in the per-item table too) and both ends of the bootstrap and Wilson
   intervals. 0/45 reads `≤ 6.5 %` (6.44 %), a lower bound such as 99.97 % reads `≥ 99.9 %`, never
   `≥ 100.0 %`, and a Wilson interval from 11.76 % to 76.93 % reads `11.7 to 77.0 %`. Point values
   (rates, ASR@k) still round to the nearest 0.1 %. A displayed interval is therefore never
   narrower than the computed one, and an interval of positive width (beyond float noise) never
   collapses on display.
6. **Reporting.** A legend line under the Overall block and under the category table says that the
   bounds and Wilson treat attempts as independent, while repeats of one item are correlated. The
   JSON report names the method per group in `asr1_method` and `asrk_method`, next to `asr1_ci95`
   and `asrk_ci95`; a one-sided bound appears there as `[0, x]` or `[y, 1]`.

## Consequences

- No report shows a collapsed interval (`[95 % CI a to a %]`); a test checks synthetic reports and
  both self-tests at 1 and 3 repeats.
- The bounds and Wilson assume independent attempts. Repeats of one item are correlated, so those
  intervals are too narrow when the group mixes items that always work with items that never do.
  The bootstrap from five items on accounts for that; below five items nothing in this rule can.
- The 5-item threshold is a convention, not a derived value. Changing it, or replacing the
  approximation in point 4 with an exact method, needs a new ADR.
- Reports written before M1.2 are not comparable line by line: their groups show bootstrap intervals
  where M1.2 shows bounds or Wilson, and their bounds and interval ends round to the nearest 0.1 %.

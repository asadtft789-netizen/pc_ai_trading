# F16 blocks statistical promotion

**Decision on 28 November 2026.** Accept commit `7febbe3f5450048071f265f9350c70cfa9b6c5e3` for independent integration review. Keep production statistical promotion disabled. The accepted bundle and its original handover are preserved. The release-hold follow-up adds an empty, source-controlled set of released protocol digests. Issuance records an inconclusive result with the F16 reason, even with otherwise passing checks. Consumption checks the current release decision, including for previously eligible certificates. Research can still record results. Only the supervisor could activate a paper strategy following a future release.

## Evidence for the hold

The accepted study tested 256 three-comparison families per design using 30 sessions, 100,000 centered circular bootstrap resamples at each of lengths 3, 5 and 10, the largest block p-value and full-family Holm correction. Under a global null every rejection is false. The release metric is the probability of **one or more false rejections in a campaign**. Its limit is 0.05/12, or 0.4167%, across at most 12 preregistered campaigns per UTC year.

| Registered null | Families with a false rejection | Observed rate | Two-sided exact 95% interval |
|---|---:|---:|---:|
| Independent Gaussian days | 3 / 256 | 1.17% | 0.24%–3.39% |
| Gaussian AR(1), lag-one correlation 0.7 | 25 / 256 | 9.77% | 6.42%–14.08% |

The serial-null interval lies entirely above the target. The independent-day interval includes the target and fails the required upper-bound check. With 30 observations, blocks of ten provide just three resampled blocks per path. More Monte Carlo draws improve numerical precision conditional on the observed window. They cannot supply the missing independent market information. Holm needs valid marginal p-values for true nulls. It cannot repair a miscalibrated marginal test.

## Three alternatives investigated

All methods test the mean of the **same paired, cost-adjusted daily increment** as the registered comparison. They preserve the one-sided target, complete Holm family and 30-session endpoint. These are offline analytic research alternatives. The deployed protocol, registered resampling settings, annual budget and consumed holdouts were never edited or used for method selection.

1. **Gaussian generalized least squares t-test.** For stationary Gaussian AR(1) errors with externally known coefficient rho and unknown scale, whiten the first observation by sqrt(1−rho²) and the remaining observations by subtracting rho times the previous value. Apply the same transformation to the intercept and use the one-sided t statistic with 29 residual degrees of freedom. Under a constant mean and the declared covariance shape this is a finite-sample test. The prototype fixes rho at 0.7 throughout. Fitting rho to the same 30 values would invalidate the stated reference distribution.
2. **Gaussian covariance upper bound.** Suppose the daily increments are jointly Gaussian, daily SD is at most s and each lag-k covariance is bounded above by s²rᵏ. Then Var(mean) ≤ V = s²[n + 2 Σ(n−k)rᵏ]/n², summing k from 1 through n−1. For a positive observed mean d, the upper-tail Gaussian probability at d/√V is conservative. For a nonpositive d use p=1. These p-values are super-uniform under the mean null and the stated envelope. This method needs independently justified bounds on both volatility and temporal covariance.
3. **Cantelli covariance upper bound.** Use the same V, but require only finite second moments and a justified covariance envelope. For d>0 use V/(V+d²), otherwise use 1. Cantelli's one-sided inequality bounds the null tail without Gaussianity. Its finite-sample guarantee can cost nearly all the power at the required threshold. A paper notional limit or nominal stop distance alone does not establish an unconditional variance or serial-covariance bound.

## Synthetic sensitivity study

The study used fresh, seeded synthetic panels. Each of four data-generating processes supplied 50,000 independent families. Each family had 30 observations for 12 correlated comparisons. The three-comparison case used its first three columns. Cross-comparison correlation was 0.8, daily marginal SD was 0.001, and positive alternatives shifted the mean by 0.001, or 10 basis points per day after the economic allowance. The SD and correlation envelopes supplied to the tests stayed fixed at 0.001 and 0.7. The fourth design deliberately violated the correlation envelope. The heavy-tailed design used standardized Student-t innovations with five degrees of freedom and 100 burn-in steps.

Measure familywise error under the global null and when only the first comparison is null. Measure power as the probability of rejecting **all** comparisons when all have the declared shift. The same generated panels feed all methods and shifts. This pairs comparisons between methods. It does not yield separate independent confirmations for each row.

| 3-comparison family | Global-null errors at rho 0.7 | One remaining true null rejected | All three effects detected | Global-null errors at rho 0.95 |
|---|---:|---:|---:|---:|
| Gaussian GLS t | 0.346% | 0.430% | 20.880% | 34.536% |
| Gaussian covariance bound | 0.310% | 0.454% | 24.594% | 11.050% |
| Cantelli covariance bound | 0 / 50,000 | 0 / 50,000 | 0 / 50,000 | 0 / 50,000 |

At rho 0.7 the partial-null error intervals are 0.375%–0.491% for GLS and 0.397%–0.517% for the Gaussian envelope. Both upper limits exceed the 0.4167% release target. Across twelve comparisons the respective full-family powers fell to 8.954% and 12.280%. Under t5 innovations the Gaussian envelope's three-comparison global-null upper limit was 0.438%. Cantelli's observed zero detections give a two-sided 95% upper power limit of 0.00738% at this effect size. Zero observed errors outside its assumptions do not establish validity there. The CSV contains counts, denominators, rates and pointwise Clopper–Pearson intervals for all 72 cells. The intervals do not adjust for selecting methods or inspecting many designs.

**Disposition.** No alternative is approved. The Gaussian calculations isolate the cost of modeling persistent dependence incorrectly. The distribution-free bound illustrates the loss of practical power when only second moments are available. With arbitrary dependence, 30 equal-mean observations can behave like a single draw. Choose a future method only if its assumptions can be independently justified at the intended horizon.

## Requirements for an independent release decision

- Before a new confirmatory campaign or final calibration, freeze the estimand, policy and data provenance, calendar, comparison geometry, method, nuisance bounds, effect, power target, seeds and review criteria. Whenever a protocol changes, use a new identity and new prospective evidence. The existing market holdouts remain consumed.
- Support distributional assumptions or external bounds with information available before enrollment. Specify how gaps, revisions, clustered volatility, structural shifts and assumption violations produce an inconclusive result. Test correlated partial-null families, including different subsets and effect strengths for up to 12 comparisons, and check comparators that share an incumbent or cash path.
- Have an independent reviewer implement the statistic and Holm decision, verify marginal validity and rerun a locked simulation plan on independent streams. Predefine simultaneous Monte Carlo confidence or an equivalent controlled assessment across the release-critical scenarios. Require upper false-rejection bounds at or below 0.05/12 and the required lower power bound. Account for method selection. A conservative design margin may be necessary to demonstrate that upper bound.
- Keep the existing 30-session campaign frozen. A longer horizon would require a separately authorized protocol, a prospectively registered new endpoint and fresh observations. Never extend a near pass. No rerun may release the original v1 findings.
- Review the exact source digest, the independent calibration artifacts and the supervisor's current routing/safety checks before a future code change could release a protocol. Do not derive release permission from a caller flag, a test double or a favorable simulation cell.

## Reproduction and verification

`handoffs/inference_sensitivity.py` uses only synthetic draws and has no production imports or database access. Run the new investigation with Python 3.12.14, NumPy 2.3.5 and SciPy 1.17.0. These are investigation dependencies separate from the application lockfile.

```sh
OPENBLAS_NUM_THREADS=1 python handoffs/inference_sensitivity.py > sensitivity.csv
```

Seed 7180243 generates independent child streams by scenario. GLS results for 204 synthetic series agreed with a separate dense inverse-covariance calculation. The two envelope calculations agreed with the variance of the mean computed directly from the full covariance matrix. Vectorized Holm decisions agreed with the repository scalar implementation on 17 families. All 72 result rows reconciled to their counts. These are single-agent implementation checks. Independent release verification remains open.

Only three affected application tests were executed in the existing verified virtual environment. All three passed. They cover incomplete calibration, refusal to issue eligible evidence with otherwise favorable inputs and refusal to consume a previously eligible certificate after a release hold. The historical 125-test milestone result belongs to the accepted commit.

```sh
python offline_suite.py test_experiment_validation.Tournament.test_real_calibration_insufficient_despite_large_effect test_experiment_validation.Tournament.test_supervisor_contract_with_synthetic_verifier_fixture test_experiment_validation.Tournament.test_old_eligible_certificate_is_blocked_at_consumption
```

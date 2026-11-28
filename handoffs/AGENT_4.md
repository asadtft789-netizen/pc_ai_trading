# Agent 4 statistical validation handoff

**Release disposition, 28 November 2026.** Phoenix has no demonstrated trading advantage. The prescribed 30-session bootstrap failed to establish its nominal error rate in two synthetic studies. The serially dependent study produced false discoveries in 25 of 256 families. An attractive shadow P&L or a small adjusted p-value alone cannot earn an eligible certificate.

Base `e955019656e5bf101982bb4f0e5623bed09dd894`. Work branch `phoenix/statistical-validation`. Target `phoenix/professional-autonomous-v1`. This change implements research registration, adjudication and a read-only certificate consumer. Production campaign enrollment, qualifying market observations and supervisor-side activation remain open.

## Historical experiments

1. Use one authoritative operational database initialized by `paper_store.init`. Pass a point-in-time panel to `historical_validation.register_dataset` before inspecting rankings or selecting features. Supply an ordered feature list, a frozen symbol universe, feed, vintage, quality evidence, feature version and maximum target/holding horizon rounded up to relevant sessions. The panel index is (symbol, offset-aware timestamp). Columns comprise the declared numeric features, label, available_at, label_end, position_end and label_available_at. Use original point-in-time vintages, corporate-action information and feature readiness from the data owner.
2. The dataset digest covers the index, names, dtypes, columns and values. The registry verifies calendar coverage per symbol, feature availability, observed holding/label horizons and exchange decision times. The final 30 eligible historical sessions are reserved by market and date across re-registrations. The ordinary exploratory review records the dates it sees and masks reservations before scoring proposals. The latest pre-registry development cut is a conservative legacy exposure watermark. Use about 24 months of consistent history when available. The normal short hourly download cannot establish a broad development claim.
3. `walk_forward` registers each experiment before fitting. Require at least 12 calendar months before the reserve. With N development sessions each of six validation windows has floor(N/7) sessions. The leading remainder stays with the expanding training prefix. Keep a gap equal to the declared maximum horizon, at least one session for equities. Crypto uses a conservative integer UTC-day ceiling. Purge training and validation rows whose target, realized position or label availability crosses the respective window end. Fit, select, scale and calibrate using the training callback only. Prediction sees a single simultaneous feature cohort at a time. The independent scorer receives the aligned predictions and labels. Store all six metrics or the failure.
4. `inspect_holdout` takes the whole frozen candidate family, keyed by specification digest. It charges every reserved date atomically before invoking any estimator, including unsuccessful or interrupted runs. Each fit receives purged, embargoed development data. Each predictor sees only feature columns. Read an existing completed report to replay the result. An interrupted inspection leaves a durable charge. A new family cannot rerank that holdout. Existing five-session learning-sidecar holdouts also retain their first inspection, and discovery excludes reserved episodes across rollover.
5. Boolean policies use the exact truth table over all 4,096 feature assignments including the guard. For parameterized policies use `register_probes` for every arm, cash and applicable ablations. The trusted policy runner receives the same ordered development probe set of up to 512 rows. The registry freezes probe inputs and outputs for the dataset, feature schema, code and complete spec. Agreement on a finite probe set is a conservative screening rule. The current strategy-worker grammar is Boolean. Nonempty model weights require a future versioned model adapter. Agent 3 owns proposal generation and fitted artifacts.

The host that loads raw data or supplies callbacks can see the underlying files. The ledger protects the supported experiment interfaces. Prior access in another database, unlogged human inspection, look-ahead hidden inside a precomputed feature or missing intraday quotes require independent provenance checks. A registration alone cannot establish that history was genuinely unseen.

## Prospective enrollment and intake

`campaign_registry.register` is the registration API. The first confirmatory registration is equity. Use the calendar start date, enrolled historical dataset, immutable risk/execution/cost/code/feature digests, per-fill allowance, universe, benchmark, adverse gap in ATR units, delay in eligible quotes, minimum economic effect, power target, null design digest and separate producer/verifier identities. Freeze the incumbent, simple baseline, challenger source, guard, parameters, weights, parent, hypothesis, deterministic bootstrap seed and complete routing generation, global fallback, target and all four regime routes. Candidate and campaign IDs are SHA-256 over canonical JSON. The incumbent must match the expected route.

Enrollment precedes the first open and fixes 30 consecutive XNYS sessions including early closes, or 30 consecutive UTC days for a later crypto research campaign. The protocol uses one common $10 allocation per hypothetical portfolio. Register one to four challengers and at most 12 comparisons. All challengers compare with cash, the frozen incumbent (cash at cold start), and the simple frozen baseline. Every guard hypothesis additionally compares to the same policy with its guard removed. A feature hypothesis registers a frozen false-input ablation. Whole policies require no artificial guard. The market benchmark is SPY or BTC/USD and is reported separately. The initial equity implementation retains the simple baseline comparison even when another control has the same return path.

A transaction charges one of 12 slots in the UTC year of registration, across all scopes. An identical retry uses the same slot. Abandonment, expiry, changed seeds and failed results never refund it. Keep this ledger and its history during backup, restore and integration. A copied or newly created database is a separate experiment history and must never be used to reset a budget.

**Agent 1 intake contract.** Submit a single matched session panel through `record_session` after its registered close and within 120 seconds. The trusted producer must continuously preserve point-in-time events. Include the event digest, actual replay input bindings, benchmark return, regime coverage in seconds, every registered arm and each preregistered scenario. Compute the bindings from the inputs the simulator actually used. `replay_inputs` defines the canonical encoding. Each arm supplies the common ordered mark grid including intraday session boundaries, net allocated equity, gross traded dollars, flat-to-flat completed episode intervals with symbol and canonical economic action-trace digest, and reconciled symbol/regime net P&L. Trace IDs must exclude the arm name, strategy name and other metadata that would make identical actions look different. Include genuine zero-trade sessions. Retain vintages, fills, fees, marks, regime observations, caps and quote eligibility behind the digests. Guard execution and unchanged routing policies must replay on the same eligible events. A late historical reconstruction cannot fill a missing receipt.

**Independent verification.** Run a second trusted implementation on the sealed event stream under the registered policy and scenario inputs. Submit its panel digests and artifact-backed data-quality, safety and assumption checks through `record_verification`. Attach the predeclared calibration study generated by `register_calibration`. Persist these artifacts under their hashes and authenticate the authority of the writer outside SQLite. The application verifies hashes, source identities, calendar, continuity, accounting relationships, replay agreement and calibration bounds. A digest cannot prove that an external verifier actually performed its work. Wait for outstanding verification before explicitly closing. The observation endpoint never moves. The daemon closes unresolved registrations after a fixed 24-hour verification deadline. An explicit close is final, even if evidence subsequently arrives.

## Inference, sufficiency and eligibility

For each comparison and every registered date d, use

```text
D[d] = (candidate net marked P&L[d] - control net marked P&L[d]
        - 0.0002 * candidate gross traded dollars[d]) / 10
```

The registered starting capital is the denominator throughout. Require at least 20 completed candidate episodes and 10 differing portfolio excursions per control. Merge overlapping candidate/control position intervals until both portfolios are flat. Count a different economic trace once per excursion. Missing dates, inadequate episodes, malformed values, degenerate variance and unsupported assumptions cannot pass.

Center the paired series on its sample mean. For each of block lengths 3, 5 and 10, draw uniformly from circular block starts and concatenate to 30 observations. Use the registered NumPy generator seed, 100,000 draws per length, batches of at most 2,048 and a one-sided inclusive tail. The p-value is (1 + null exceedances)/(100,001). Use the largest of the three p-values. Sort the entire preregistered family, apply Holm step-down with monotone adjusted p-values, and allocate p=1 to unmeasured members. Require every declared comparison for a candidate to pass at 0.05/12. There is no favorable-block selection or trade-date windowing.

The adjudicator checks positive net P&L, maximum intraday marked drawdown strictly below 10%, and positive incremental returns in at least two of three consecutive ten-session thirds for each required comparison. It reports per-regime and per-symbol returns, seconds by regime, symbol exposure, time in the market, turnover and largest profitable-day share. The stressed per-fill allowance is max(2 × base, base + 2 bp). Stress, delayed entry and adverse gap replays must remain profitable with drawdown below 10%. Check every immediate one-coordinate neighbor on the plan's approved parameter grids. The frozen v1 material-fragility convention requires at least half of base profit for every neighbor, in addition to the stress risk limits. Delay size, gap size, detectable effect and target power must be selected before the campaign. These conventions are research decisions.

The calibration study draws independent complete families of 30-session stationary Gaussian AR(1) differentials with an explicit cross-comparison common factor. Simulate both a global null and the declared positive shift under the actual registration seed, family size and full confirmatory resampling procedure. Estimate probability of any false rejection and probability all required alternatives reject. Compute one-sided 97.5% Clopper–Pearson bounds for the two rates. Eligibility requires the null upper bound at or below 0.05/12, the power lower bound at or above the registered target and independently supported model assumptions. These bounds concern Monte Carlo uncertainty conditional on that generator. Global-null simulation does not establish strong control for all partial-null configurations, nonstationarity, heavy tails, long memory, a different family geometry or future markets. Keep such cases inconclusive until additional independent calibration supports their assumptions.

The final report stores all p-values, adjusted results, daily increments, thirds, provenance and failure reasons. `supported_claim` checks structured historical metrics, measured shadow net P&L, attributed broker-confirmed gross lot P&L and certificate-backed prospective superiority. Free text, future-profit claims and broker net claims have no automatic authority. Use separate evidence classes in Agent 3 memory and the dashboard. Shadow drift compares the two fixed 15-session halves descriptively. Paper drift uses nonoverlapping 28-day UTC blocks, two fixed 14-day halves, at least 20 closed lots per half, and gross P&L per complete lot. A decline of at least 20% from a positive first-half mean opens an investigation. Drift has no routing authority.

## Consumer contract for the integration manager and Agent 2

Read the issued certificate by digest from the same authoritative database. Reconstruct the complete candidate identity and exact current parent and versioned routing manifest. Call `verify_eligibility` for the individual candidate. This predicate reads committed evidence. It does not acquire a supervisor fence, consume a certificate, schedule activation or write strategy state. In the supervisor's own atomic compare-and-swap, recheck the expected complete manifest and generation, replay protection, current data/safety gates, expiry and applicability. The supervisor alone may schedule an accepted strategy at the next eligible boundary and preserve the original exit policy on existing lots. A stale, failed, inconclusive, mismatched or missing certificate cannot authorize a validated paper route. Crypto remains research-only. The historical review and five-session learning-sidecar pointer are exploratory.

Agent 3 can use the dataset, fit, holdout and enrollment APIs directly. The CLI accepts an existing initialized operational database and JSON arguments matching the APIs.

```sh
python experiment_cli.py --db PATH register registration.json
python experiment_cli.py --db PATH calibrate design.json
python experiment_cli.py --db PATH show CAMPAIGN_SHA256
python experiment_cli.py --db PATH close CAMPAIGN_SHA256
python experiment_cli.py --db PATH check-certificate expected.json
```

Production integration still requires Agent 1's canonical quote/fee/mark producer and independent replay, the data owner's original-vintage and calibration-model evidence, Agent 3's scheduling and artifact retention, and the supervisor's fenced consumer. Operational database access control, tamper-resistant external retention and restore reconciliation are integration requirements. No broker account or Oracle credentials were used.

## Reproducible verification

Debian 13, CPython 3.12.14, disposable SQLite, no order transport. Create a fresh virtual environment, activate it and run the following from the checkout.

```sh
python -m pip install -r requirements-verified.txt
python -m pip check
python offline_suite.py
python offline_suite.py test_experiment_validation test_campaign_statistics test_learning_loop
python campaign_calibration.py --replicates 256 --family 3
```

The unchanged base passed 104 tests in 4.646 seconds. The branch passed 125 application tests in 6.977 seconds. The targeted command passed 51 in 3.994 seconds and includes a separate literal block-index bootstrap oracle, matched-factor/zero-variance examples, full-family correction, genuinely missing dates, retries after crashes, two writers competing for the last slot, shared equity/crypto budgets, altered source bindings, regime and drawdown marks, frozen holdouts and stale routing. The eligible consumer branch uses an explicitly injected synthetic verification study. It is a contract test. The real calibration path returns inconclusive for insufficient precision or unacceptable false discoveries.

An intermediate end-to-end check failed because numeric block-length keys sorted differently after a JSON round trip, making a stored certificate unverifiable. Canonical string keys now preserve the digest across persistence and replay.

| 256 independent families per design | Families with any false rejection | Upper false-rejection bound | Families detecting all alternatives | Lower power bound |
|---|---:|---:|---:|---:|
| Independent daily innovations, rho = 0 | 3 (1.17%) | 3.39% | 254 (99.22%) | 97.21% |
| Serial persistence, rho = 0.7 | 25 (9.77%) | 14.08% | 168 (65.63%) | 59.46% |

Both rows use daily SD 0.001, cross-comparison correlation 0.8, an alternative shift of 0.001 after costs, simulation seed 314159 and bootstrap seed 771. The target campaign error rate is approximately 0.417%. Neither design qualifies. In particular the serial case shows that 30 sessions and these fixed blocks can substantially understate uncertainty even after Holm. Preserve all negative results. Any investigation of a longer window, alternate test or different assumptions needs a new prospectively approved protocol and fresh evidence. Baseline and branch tests, synthetic profits and paper-gross episode records establish no market advantage.

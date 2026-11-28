# Agent 5 handoff

WP7 work starts from the verified common commit `e955019656e5bf101982bb4f0e5623bed09dd894` on `phoenix/cloud-infrastructure`. Integrate by pull request into `phoenix/professional-autonomous-v1`. No Oracle connection, deployment, credentials, broker orders or trading-mode changes took place.

## Evidence

- Clean Linux CPython 3.12 checkout with `requirements-verified.txt` installed. `pip check` passed and the exact baseline command passed all 104 tests in 7.768 seconds. The first sandbox run executed 104 tests and returned one loopback socket permission error. The clean rerun used the suite's own offline network guard with local sockets available.
- Credential-stripped whole-tree discovery reproduced 601 tests, 371 errors and 52 skips. The errors include missing vendored test dependencies and tool-local imports. The imported MT5 dashboard collides in `sys.modules` with the application package during mixed discovery. The application suite keeps the affected tests and has an explicit, separate `python offline_suite.py --vendored` diagnostic for the imported tree. Whole-tree discovery is unsuitable as a release check and the vendor suite remains unqualified.
- The extended application suite, static checks, encrypted-storage drill and soak smoke results are recorded below once verified.

## Integration contracts

The operational dashboard uses supervisor-observed account equity, buying power, restriction, counts and holding quantities with observation timestamps. It distinguishes those observations from reconciled local order intents and positions. F24 remains open in the absence of authoritative marked allocated equity and confirmed fees. Agent 1 should supply versioned values with timestamps, provenance, currency and uncertainty through the operational read model. Keep the allocation ceiling and broker account equity visible as distinct figures.

The dashboard shows observed research scopes, receipt sequences, batches, strategy identities and unresolved failure statuses. Current historical champion or challenger records are development states. Agent 4 should supply eligible activated strategy scopes, active version, evidence-certificate identity, status, uncertainty and next pending campaign boundary through the authoritative read model. Unknown and stale readings remain visible. No separate confirmed accounting or activation branch contract was assumed.

## Release and configuration blockers

- Follow `DEPLOYMENT.md` and `RECOVERY.md`. Use a disposable Ubuntu 24.04 system to exercise real systemd/cgroup enforcement, role users, authenticated mobile access, measured peak usage, restart handling and versioned staging/rollback. Oracle remains outside this assignment. A separate authorized operator must verify existing identities, coverage and release gates before cutover.
- A dedicated paper observation account and independent market-data credentials, if required, are operator-supplied. Confirm broker capabilities and position protection independently. The adapter remains observation only. There is no validated activation or entry authorization in this branch.
- Configure the authorized encrypted off-host backup target and the external heartbeat receiver through operator-controlled settings. Measure restore time and validate sustained backup and outage alert delivery. Record the authorized private mobile path and test it from Android.
- Run an actual 48-hour integrated soak on the clean, pinned final candidate and a persistent Linux runner. This task has no confirmed persistent runner or final candidate. The component soak on `aa8149a` does not qualify. Use `python integrated_soak.py /private/evidence/rc1 --revision FULL_SHA`. Preserve the directory when the runner restarts. The resumed observation starts a fresh continuous qualification interval. The harness runs two independent workers on the same disposable ledger, checks the dashboard read model, takes periodically restored backup sets, and injects broker outages and supervisor kills using fake inputs. `--smoke` is an independent evidence mode and cannot qualify. The remaining release tests include actual systemd runtime enforcement, production-resolution market data and a separately authorized observation account.

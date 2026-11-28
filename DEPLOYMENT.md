# Oracle deployment status
Release status is blocked. The existing Oracle services were inventoried read-only on 2026-11-27. The checkout was 5a5773643e2690b0a301b11aa079781a05ec9e89, with research and supervise processes owned by ubuntu, Ubuntu 24.04.5 on x86_64, 2 CPUs, 952 MiB RAM, 488 MiB available, zero swap and 41 GiB free. Their loaded source was unverified. At 12:25 PKT the local ledger held zero intents, fills and lots and the .env allowed paper entries. This does not establish the broker's holdings.

Use a separate checkout of the intended exact commit. Install requirements-verified.txt into a separate environment, run python offline_suite.py, and retain the platform, lockfile and test results. Use python offline_soak.py NEW_EVIDENCE_DIRECTORY from a clean immutable checkout. Results belong outside the checkout. --smoke runs one cycle and cannot qualify the elapsed gate.

Legacy scripts and DEPLOY.md predate these gates. Keep them out of the release workflow. Before service changes follow IMPLEMENTATION_PLAN.md sections 35–36 and RELEASE_CHECKLIST.md. No deployable cutover has been certified. Retain the existing supervisor until identity, ownership, protection, backup and rollback evidence are available. Do not attach this observation-only gateway to a position that depends on the old supervisor.

## Ubuntu 24.04 release tooling

The canonical entry point is `deploy/phoenix/phoenix-control`. Use it on a disposable Ubuntu 24.04 system first. An authorized operator with root access can stage an independently reviewed, exact 40-character Git commit:

```sh
sudo bash deploy/phoenix/phoenix-control stage /path/to/trusted/checkout FULL_SHA
sudo bash deploy/phoenix/phoenix-control install-units FULL_SHA
sudo phoenix-control activate FULL_SHA
```

Staging uses CPython 3.12 and the exact dependency versions in `requirements-verified.txt`. It uses wheels, checks dependency consistency, and runs a disposable, credential-free configuration and database preflight. Release directories in `/opt/phoenix/releases` are immutable to service accounts. Package integrity currently depends on the trusted package index and TLS. The lock records versions, and a separately verified wheel hash manifest remains an additional release control. Activate and rollback select code offline and reject a database with a different schema version. Each command leaves unit startup to the authorized operator. Install new or changed units only during the approved maintenance window.

Configuration lives under root-owned `/etc/phoenix` with mode 0700. Use mode 0600 for environment and credential files.

| File | Settings |
|---|---|
| `supervisor.env` | Dedicated paper observation credentials via `ALPACA_API_KEY` and `ALPACA_API_SECRET`. Optional paper endpoint override must equal `https://paper-api.alpaca.markets`. |
| `research-data.env` | Independently authorized market-data credentials if required by the feed. Keep supervisor credentials and model keys out of this file. |
| `dashboard.env` | Optional `PHOENIX_DASHBOARD_HOSTS` with space-separated authenticated VPN proxy hostnames. |
| `backup.env` | Optional `PHOENIX_BACKUP_REMOTE=encrypted-remote:phoenix/dedicated-prefix`. Use `systemctl edit phoenix-backup.service` to add `LoadCredential=rclone.conf:/etc/phoenix/rclone.conf` and install `rclone`. |
| `monitor.env` | Optional `PHOENIX_HEARTBEAT_URL=https://authorized-service.example/token` for an HTTPS dead-man switch with a `/fail` endpoint. |

The services use separate Linux identities. Research and supervision share SQLite for their existing domain-specific transactions. The supervisor process holds a lifetime ledger-local fence and its broker operations retain the shorter transaction fences used by the execution policy. The dashboard, backup, and monitor see the state directory through read-only mounts. The dashboard has no broker credentials. Supervisor CPU weight and memory protection outrank research. Research admits heavy evaluation after the existing 256/384 MiB hysteresis and at least 1 GiB free on the state volume. The limits are initial settings requiring measurement before Oracle release. The systemd journal namespace stores redacted events with separate time-based and size-based retention.

The dashboard listens on `127.0.0.1:8787`. For Android use, an approved VPN reverse proxy can forward only authorized users to that address over TLS. Alternatively, use port forwarding in an Android SSH client with the verified host key and open `http://127.0.0.1:8787` on the handset. Do not publish port 8787 or enable public sharing. Check that a client outside the private path cannot reach it and verify the private session from Android. Service operation, health sampling, log rotation and backups run on Linux independently of the handset.

After approved cutover gates and paper observation configuration, explicitly enable the supervisor, research, dashboard and the backup and monitor timers. Confirm loaded revision, heartbeats, evidence sequence, reconciliation and resource usage. Observe with `journalctl --namespace=phoenix`. The external heartbeat requires an independent receiver and an observed missed-ping test. A locally logged 2xx only confirms the endpoint accepted the request. Historical Oracle values above require fresh inventory in a separately authorized session.

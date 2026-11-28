# Disposable Ubuntu runtime acceptance

Status is unverified. Run only on an authorized fresh Ubuntu 24.04 VM with systemd as PID 1, delegated cgroup v2 CPU, memory and pids controllers, root access, Python 3.12 with venv, Git, SQLite, and util-linux. Use two reviewed full revisions that include the canonical tools and share an explicitly supported operational **and** learning schema. One must be the pinned integration candidate. Capture the two SHAs, VM identity, package lock and outputs. Use disposable data and no broker, model, monitoring or storage credentials. No production databases or network paths are needed.

## Confirm the environment and stage both releases

```sh
set -eu
test "$(ps -p 1 -o comm=)" = systemd
test "$(stat -fc %T /sys/fs/cgroup)" = cgroup2fs
for controller in cpu memory pids; do
    grep -qw "$controller" /sys/fs/cgroup/cgroup.controllers
done
. /etc/os-release
test "$ID" = ubuntu
test "$VERSION_ID" = 24.04
python3.12 --version
sudo -v

checkout=/path/to/reviewed/checkout
previous=REPLACE_WITH_FULL_COMPATIBLE_SHA
candidate=REPLACE_WITH_FULL_INTEGRATED_SHA
cd "$checkout"
sudo bash deploy/phoenix/verify-state-isolation.sh --fixture
sudo bash deploy/phoenix/phoenix-control stage "$checkout" "$previous"
sudo bash deploy/phoenix/phoenix-control stage "$checkout" "$candidate"
sudo bash deploy/phoenix/phoenix-control install-units "$candidate"
sudo install -m 0600 /dev/null /etc/phoenix/supervisor.env
```

Disallow external egress for network-facing units before starting any service. Keep the VM firewall isolated as a separate prerequisite. The dashboard may use its existing loopback allowance.

```sh
for role in supervise research backup monitor; do
    sudo install -d -m 0755 "/etc/systemd/system/phoenix-$role.service.d"
    printf '[Service]\nIPAddressDeny=any\nIPAddressAllow=\n' |
        sudo tee "/etc/systemd/system/phoenix-$role.service.d/10-acceptance.conf" >/dev/null
done
sudo systemctl daemon-reload
sudo phoenix-control activate "$previous"
sudo phoenix-control activate "$candidate"
test "$(readlink -f /opt/phoenix/current)" = "/opt/phoenix/releases/$candidate"
test "$(readlink -f /opt/phoenix/previous)" = "/opt/phoenix/releases/$previous"
```

## Enforce identities, limits and recovery

```sh
sudo systemctl start phoenix-supervise.service phoenix-research.service phoenix-dashboard.service
for service in phoenix-supervise.service phoenix-research.service phoenix-dashboard.service; do
    sudo systemctl is-active "$service"
    systemctl show "$service" -p User -p Group -p MainPID -p ControlGroup -p NoNewPrivileges -p ReadWritePaths
    pid=$(systemctl show "$service" -p MainPID --value)
    group=$(systemctl show "$service" -p ControlGroup --value)
    ps -o uid,gid,args -p "$pid"
    cat "/proc/$pid/cgroup"
    sudo cat "/sys/fs/cgroup$group/memory.max" "/sys/fs/cgroup$group/cpu.max" "/sys/fs/cgroup$group/pids.max"
done
```

Compare the cgroup values with each service's unit and confirm the operating UIDs match the intended separate accounts. Baseline limits are 256 MiB / 90% of one CPU / 24 tasks for supervision and 384 MiB / 75% / 24 for research. Any change in the integrated candidate needs its corresponding expected values recorded. On the isolated VM, use a trusted bounded workload in a separate transient unit with the same properties to confirm throttling or memory failure. For example:

```sh
sudo systemd-run --unit=phoenix-acceptance-cpu --property=CPUQuota=75% --wait --collect \
    python3.12 -c 'import time; end=time.monotonic()+4; cpu=time.process_time(); exec("while time.monotonic() < end: pass"); assert time.process_time()-cpu < 3.6'
if sudo systemd-run --unit=phoenix-acceptance-memory --property=MemoryMax=64M \
    --property=MemorySwapMax=0 --wait python3.12 -c 'bytearray(128*1024*1024)'; then
    exit 1
fi
test "$(systemctl show phoenix-acceptance-memory.service -p Result --value)" = oom-kill
```

When Agent 2's real separation is integrated and the authorization database and display snapshot exist, run the installed check. Use the final agreed paths if they differ.

```sh
sudo bash deploy/phoenix/verify-state-isolation.sh --installed --disposable \
    /var/lib/phoenix/authorization/authorization.sqlite /var/lib/phoenix/research \
    /etc/phoenix/supervisor.env /var/lib/phoenix/telemetry/routing/status.json
pid=$(systemctl show phoenix-supervise.service -p MainPID --value)
sudo runuser -u phoenix-research -- test ! -r "/proc/$pid/environ"
before=$pid
sudo systemctl kill --kill-whom=main --signal=SIGKILL phoenix-supervise.service
sleep 35
after=$(systemctl show phoenix-supervise.service -p MainPID --value)
test "$after" -gt 0
test "$after" != "$before"
sudo systemctl is-active phoenix-research.service
```

Record any permission failure as a release blocker. The unmodified shared state layout is expected to fail the authorization-directory check. Review journal events and distinguish the intentionally unavailable broker from a process crash. Verify binding identities, registry records, receipt cursors and absence of any credential or external request in the acceptance artifacts.

## Rehearse rollback and database compatibility

An active-unit rollback attempt must fail and preserve the current SHA. Stop the disposable units and verify compatible rollback changes only the release pointer.

```sh
if sudo phoenix-control rollback "$previous"; then exit 1; fi
test "$(readlink -f /opt/phoenix/current)" = "/opt/phoenix/releases/$candidate"
sudo systemctl stop phoenix-backup.timer phoenix-monitor.timer phoenix-backup.service phoenix-monitor.service \
    phoenix-dashboard.service phoenix-research.service phoenix-supervise.service
state=/var/lib/phoenix/state
before=$(sudo sha256sum "$state/operational.sqlite" "$state/learning_loop.sqlite")
sudo phoenix-control rollback "$previous"
test "$(readlink -f /opt/phoenix/current)" = "/opt/phoenix/releases/$previous"
test "$(sudo sha256sum "$state/operational.sqlite" "$state/learning_loop.sqlite")" = "$before"
sudo phoenix-control activate "$candidate"
test "$(readlink -f /opt/phoenix/current)" = "/opt/phoenix/releases/$candidate"
```

Use the final integrated store paths if Agent 2 splits state. On a second copy of the disposable VM, with all writers stopped and the candidate selected, exercise incompatible schema refusal and verify a restore into a new directory.

```sh
recovery=$(sudo mktemp -d /var/tmp/phoenix-acceptance.XXXXXX)
sudo env PHOENIX_REVISION="$candidate" /opt/phoenix/current/venv/bin/python \
    /opt/phoenix/current/backup_sets.py backup "$state/operational.sqlite" "$recovery" "$recovery/status.json"
name=$(sudo python3.12 -c 'import json,sys; print(json.load(open(sys.argv[1]))["snapshot"])' "$recovery/status.json")
sudo sqlite3 "$state/operational.sqlite" 'PRAGMA user_version=999999;'
if sudo phoenix-control rollback "$previous"; then exit 1; fi
test "$(readlink -f /opt/phoenix/current)" = "/opt/phoenix/releases/$candidate"
sudo /opt/phoenix/current/venv/bin/python /opt/phoenix/current/backup_sets.py \
    restore "$recovery/$name" "$recovery/restored"
sudo sqlite3 "$recovery/restored/operational.sqlite" 'PRAGMA user_version; PRAGMA integrity_check;'
sudo sqlite3 "$recovery/restored/learning_loop.sqlite" 'PRAGMA integrity_check;'
```

Compare both schemas, store binding identifiers, learning receipts, experiment ledger digests, budgets, and the certificate-consumption fence with the pre-fault inventory. Require explicit learning-schema compatibility evidence because the current selector checks only the operational version. Record runtime acceptance separately from the elapsed-time soak.

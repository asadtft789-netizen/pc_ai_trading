"""Record real elapsed integrated acceptance on one clean, pinned Linux release."""
import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import sqlite3
import subprocess
import sys
import time

from backup_sets import backup
from dashboard.paper_view import read_view
from operational import status

ROOT = Path(__file__).resolve().parent
REQUIRED_SECONDS = 48 * 3600
MAX_GAP = 45
ROLES = ("supervise", "research")


def git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()


def assert_revision(revision):
    if git("rev-parse", "HEAD") != revision or git("status", "--porcelain", "--untracked-files=all"):
        raise ValueError("soak requires the specified clean checkout")


def stop(process):
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3)


def record(handle, **payload):
    handle.write(json.dumps({"at": datetime.now(timezone.utc).isoformat(), **payload}) + "\n")
    handle.flush()
    os.fsync(handle.fileno())


def read_last(handle):
    handle.seek(0)
    previous = {}
    for line in handle:
        if not line.endswith("\n"):
            raise ValueError("interrupted evidence record requires a new evidence directory")
        previous = json.loads(line)
    return previous


def run(directory, revision, smoke):
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("full release SHA required")
    assert_revision(revision)
    if directory.is_relative_to(ROOT):
        raise ValueError("evidence must be outside the checkout")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.stat().st_mode & 0o077:
        raise ValueError("evidence directory must have private permissions")
    with (directory / "runner.lock").open("ab") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with (directory / "events.jsonl").open("a+", encoding="utf-8") as log:
            previous = read_last(log)
            if previous and (previous["revision"] != revision or previous["smoke"] != smoke):
                raise ValueError("revision or evidence mode changed")
            if previous.get("event") in ("accepted", "smoke_passed"):
                return 0
            if previous.get("event") == "failed":
                raise ValueError("failed run requires a new evidence directory")
            return observe(directory, revision, smoke, log, previous)


def observe(directory, revision, smoke, log, previous):
    database = directory / "soak.sqlite"
    backup_dir = directory / "backups"
    backup_dir.mkdir(mode=0o700, exist_ok=True)
    fault = directory / "broker-down"
    fault.unlink(missing_ok=True)
    env = {"PATH": os.defpath, "HOME": str(directory), "PHOENIX_SOAK_DATABASE": str(database),
           "PHOENIX_REVISION": revision, "PYTHON_DOTENV_DISABLED": "1", "PYTHONUNBUFFERED": "1",
           "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1"}
    workers = {}
    restarts = 0
    process_started = last_observation = time.clock_gettime(time.CLOCK_BOOTTIME)
    window_started = process_started
    common = {"revision": revision, "smoke": smoke}
    # A resumed run keeps prior evidence and state. A fresh continuous interval starts at the first healthy sample.
    record(log, **common, event="resumed" if previous else "started", continuous_seconds=0)
    next_fault = process_started + (8 if smoke else 600)
    next_backup = process_started
    fault_started = None
    observed_outage = False
    verified_faults = 0
    first_healthy = False
    with ExitStack() as stack:
        outputs = {role: stack.enter_context((directory / (role + ".log")).open("ab")) for role in ROLES}

        def start(role):
            process = subprocess.Popen([sys.executable, "-m", "soak_fixture", role, str(database)],
                                       cwd=ROOT, env=env, stdout=outputs[role], stderr=subprocess.STDOUT)
            workers[role] = process

        try:
            for role in ROLES:
                start(role)
            while True:
                time.sleep(2 if smoke else 5)
                current = time.clock_gettime(time.CLOCK_BOOTTIME)
                recorded_gap = current - last_observation
                last_observation = current
                if recorded_gap > MAX_GAP:
                    raise RuntimeError("sampling_deadline_exceeded")
                assert_revision(revision)
                if current >= next_fault and fault_started is None:
                    workers["supervise"].kill()
                    workers["supervise"].wait(timeout=3)
                    fault.touch()
                    fault_started = current
                    observed_outage = False
                    start("supervise")
                    restarts += 1
                    record(log, **common, event="injected_supervisor_stop_and_broker_outage")
                if any(process.poll() is not None for process in workers.values()):
                    raise RuntimeError("unexpected_worker_exit")
                try:
                    health = read_view("health", path=database)
                except (OSError, ValueError, sqlite3.Error):
                    if current - process_started <= 30:
                        continue
                    raise
                if health["uncertain_orders"] or health["entries"] != "unreleased":
                    raise RuntimeError("unexpected_order_state")
                if health["research_liveness"] != "current":
                    if current - process_started <= 30:
                        continue
                    raise RuntimeError("research_heartbeat_missing")
                if fault_started is not None:
                    if health["snapshot"] == "unknown":
                        observed_outage = True
                        fault.unlink(missing_ok=True)
                    if observed_outage and health["snapshot"] == "ok":
                        verified_faults += 1
                        fault_started = None
                        next_fault = current + (600 if not smoke else 3600)
                        record(log, **common, event="verified_outage_and_restart", verified_faults=verified_faults)
                    elif current - fault_started > 25:
                        raise RuntimeError("supervisor_fault_recovery_deadline")
                elif health["reconciliation_freshness"] != "current" or health["snapshot"] != "ok":
                    if current - process_started <= 30:
                        continue
                    raise RuntimeError("reconciliation_unhealthy")
                if not first_healthy:
                    first_healthy = True
                    window_started = current
                if current >= next_backup and fault_started is None:
                    backup(database, backup_dir, revision)
                    next_backup = current + 3600
                readings = status(database, directory / "missing-backup-status")["resources"]
                elapsed = current - window_started
                record(log, **common, event="sample", continuous_seconds=elapsed,
                       sample_gap_seconds=recorded_gap, supervisor_restarts=restarts,
                       verified_faults=verified_faults, learning_sequence=health["learning_sequence"],
                       supervisor=health["snapshot"], resources=readings)
                if fault_started is None and verified_faults and (smoke or elapsed >= REQUIRED_SECONDS):
                    record(log, **common, event="smoke_passed" if smoke else "accepted",
                           continuous_seconds=elapsed, verified_faults=verified_faults)
                    return 0
        except Exception as error:
            record(log, **common, event="failed", reason=type(error).__name__, detail=str(error))
            raise
        finally:
            for process in workers.values():
                stop(process)
            fault.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence_directory", type=Path)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--smoke", action="store_true")
    args = parser.parse_args()
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(130))
    return run(args.evidence_directory.resolve(), args.revision, args.smoke)


if __name__ == "__main__":
    raise SystemExit(main())

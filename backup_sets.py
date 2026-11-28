"""Snapshot and restore a bound pair of Phoenix databases."""
import argparse
from contextlib import closing
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import tempfile

from operational import write_status
from paper_store import db, get, now
from sqlite_snapshot import copy_verified

PAIR = ("operational.sqlite", "learning_loop.sqlite")
NAME = re.compile(r"[0-9]{8}T[0-9]{12}Z")
REVISION = re.compile(r"[0-9a-f]{40}")


def digest(path):
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def pair_state(operational, learning):
    with db(operational) as connection:
        owner, store = (get(connection, key) for key in ("learning_owner", "learning_store"))
        schema = connection.execute("PRAGMA user_version").fetchone()[0]
        consumed = int(get(connection, "learning_consumed_seq", "0"))
    with closing(sqlite3.connect(learning.as_uri() + "?mode=ro", uri=True)) as connection:
        connection.execute("BEGIN")
        identities = {key: json.loads(value) for key, value in connection.execute(
            "SELECT k,v FROM ll_meta WHERE k IN ('owner','store_id')")}
        receipts = connection.execute("SELECT coalesce(max(seq),0) FROM ll_receipts").fetchone()[0]
    if (not owner or not store or identities != {"owner": owner, "store_id": store}
            or not 0 <= consumed <= receipts):
        raise ValueError("backup pair has mismatched identities or receipts")
    return {"schema": schema, "operational_sequence": consumed, "learning_sequence": receipts}


def restore(source, destination):
    manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("format") != 1 or not REVISION.fullmatch(manifest.get("revision", "")):
        raise ValueError("unsupported backup manifest")
    destination.mkdir(mode=0o700)
    try:
        for name in PAIR:
            path = source / name
            if digest(path) != manifest["sha256"][name]:
                raise ValueError("backup checksum mismatch")
            copy_verified(path, destination / name)
        if pair_state(*(destination / name for name in PAIR)) != manifest["state"]:
            raise ValueError("backup state mismatch")
    except Exception:
        shutil.rmtree(destination)
        raise


def backup(database, root, revision):
    if not REVISION.fullmatch(revision):
        raise ValueError("exact release SHA required")
    root = root.resolve()
    # Two complete copies coexist during the restore drill.
    estimated_bytes = sum(source.stat().st_size + (
        wal.stat().st_size if (wal := Path(str(source) + "-wal")).exists() else 0)
        for source in (database, database.with_name(PAIR[1])))
    if shutil.disk_usage(root).free < (2 * estimated_bytes + (1 << 30)):
        raise OSError("insufficient space for backup and restore verification")
    clock = datetime.now(timezone.utc)
    name = clock.strftime("%Y%m%dT%H%M%S%fZ")
    with tempfile.TemporaryDirectory(prefix=".backup-", dir=root) as work:
        staging = Path(work) / "snapshot"
        staging.mkdir(mode=0o700)
        sources = (database, database.with_name(PAIR[1]))
        # Learning receipts are committed before the operational checkpoint; take that store last.
        for source, target in zip(sources, PAIR):
            copy_verified(source, staging / target)
        manifest = {"format": 1, "revision": revision, "at": clock.isoformat(),
                    "state": pair_state(*(staging / n for n in PAIR)),
                    "sha256": {n: digest(staging / n) for n in PAIR}}
        write_status(staging / "manifest.json", manifest)
        restore(staging, Path(work) / "restore-check")
        destination = root / name
        staging.rename(destination)
        descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    return destination


def keep_names(names, today, *, hourly=False):
    kept, daily, weekly = set(), set(), set()
    for name in sorted(set(names), reverse=True):
        if not NAME.fullmatch(name):
            continue
        try:
            stamp = datetime.strptime(name, "%Y%m%dT%H%M%S%fZ").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if stamp > today:
            continue
        date = stamp.date()
        week = date.isocalendar()[:2]
        if hourly and today - stamp <= timedelta(hours=25):
            kept.add(name)
        if len(daily) < 7 and date not in daily:
            kept.add(name)
            daily.add(date)
        if len(weekly) < 4 and week not in weekly:
            kept.add(name)
            weekly.add(week)
    return kept


def remote_run(config, *args):
    return subprocess.run(["rclone", "--config", str(config), *args], check=True, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=120).stdout


def encrypted_remote(remote, config):
    if not re.fullmatch(r"[A-Za-z0-9_-]+:[A-Za-z0-9_/-]+", remote) or ".." in remote:
        raise ValueError("a dedicated rclone remote and prefix are required")
    name = remote.split(":")[0]
    definitions = json.loads(remote_run(config, "config", "dump"))
    if definitions.get(name, {}).get("type") != "crypt":
        raise ValueError("offsite backup remote must use rclone crypt")


def upload(snapshot, remote, config):
    encrypted_remote(remote, config)
    prefix = remote.rstrip("/")
    target = f"{prefix}/{snapshot.name}"
    remote_run(config, "copy", "--immutable", str(snapshot), target)
    remote_run(config, "check", "--one-way", "--download", str(snapshot), target)
    remote_run(config, "copyto", "--immutable", str(snapshot / "manifest.json"), target + "/verified.json")


def restore_remote(remote, config, name, destination):
    encrypted_remote(remote, config)
    if not NAME.fullmatch(name):
        raise ValueError("invalid snapshot identity")
    with tempfile.TemporaryDirectory(prefix=".remote-restore-", dir=destination.parent) as work:
        source = Path(work)
        remote_run(config, "copy", f"{remote.rstrip('/')}/{name}", str(source))
        if (source / "verified.json").read_bytes() != (source / "manifest.json").read_bytes():
            raise ValueError("offsite manifest was not verified")
        restore(source, destination)


def prune_remote(remote, config):
    encrypted_remote(remote, config)
    prefix = remote.rstrip("/")
    names = [name.rstrip("/") for name in remote_run(config, "lsf", "--dirs-only", prefix).splitlines()
             if NAME.fullmatch(name.rstrip("/"))]
    verified = []
    # Inventory each candidate with a bounded read-only listing. Partial uploads stay available for investigation.
    for name in names:
        files = set(remote_run(config, "lsf", "--files-only", f"{prefix}/{name}").splitlines())
        if files == {*PAIR, "manifest.json", "verified.json"}:
            verified.append(name)
    keep = keep_names(verified, datetime.now(timezone.utc))
    for name in sorted(set(verified) - keep):
        remote_run(config, "purge", f"{prefix}/{name}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    save = actions.add_parser("backup")
    save.add_argument("database", type=Path)
    save.add_argument("root", type=Path)
    save.add_argument("status", type=Path)
    load = actions.add_parser("restore")
    load.add_argument("source", type=Path)
    load.add_argument("destination", type=Path)
    actions.add_parser("prune-remote")
    remote_restore = actions.add_parser("restore-remote")
    remote_restore.add_argument("name")
    remote_restore.add_argument("destination", type=Path)
    args = parser.parse_args()
    remote = os.getenv("PHOENIX_BACKUP_REMOTE")
    credentials = Path(os.getenv("CREDENTIALS_DIRECTORY", "/etc/phoenix"))
    config = Path(os.getenv("PHOENIX_RCLONE_CONFIG", credentials / "rclone.conf"))
    if args.action == "restore":
        restore(args.source.resolve(strict=True), args.destination)
        return 0
    if args.action == "restore-remote":
        restore_remote(remote or "", config, args.name, args.destination)
        return 0
    if args.action == "prune-remote":
        prune_remote(remote or "", config)
        return 0
    report = {"state": "failed", "attempted_at": now(), "offsite": "failed" if remote else "unconfigured"}
    with (args.root / ".lock").open("ab") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            snapshot = backup(args.database.resolve(), args.root, os.environ["PHOENIX_REVISION"])
            report.update(state="verified", verified_at=now(), snapshot=snapshot.name)
            candidates = [path for path in args.root.iterdir()
                          if NAME.fullmatch(path.name) and path.is_dir() and not path.is_symlink()]
            keep = keep_names([path.name for path in candidates], datetime.now(timezone.utc), hourly=True)
            for path in candidates:
                if path.name not in keep:
                    shutil.rmtree(path)
            if remote:
                upload(snapshot, remote, config)
                report["offsite"] = "verified"
        except (OSError, KeyError, ValueError, sqlite3.Error, subprocess.SubprocessError) as exc:
            report["reason"] = type(exc).__name__
        write_status(args.status, report)
    print(json.dumps({"event": "backup_attempt", **report}))
    return int(report["state"] != "verified" or "reason" in report)


if __name__ == "__main__":
    raise SystemExit(main())

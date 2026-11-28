"""Local operational telemetry and an optional HTTPS dead-man switch."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler

from paper_store import db, get, now


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def freshness(value, seconds):
    try:
        observed = datetime.fromisoformat(value)
        age = (datetime.now(timezone.utc) - observed).total_seconds()
        return "current" if -2 <= age <= seconds else "stale"
    except (TypeError, ValueError, OverflowError):
        return "unknown"


def write_status(path, payload):
    path = Path(path)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                     prefix=".status-", delete=False) as handle:
        temporary = Path(handle.name)
        try:
            json.dump(payload, handle, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
            os.fchmod(handle.fileno(), 0o640)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def read_status(path):
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def resources(path):
    mem = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        name, value = line.split(":", 1)
        if name in ("MemTotal", "MemAvailable"):
            mem[name] = int(value.split()[0]) // 1024
    disk = shutil.disk_usage(Path(path).parent)
    sizes = {}
    for name, file in {"operational": Path(path), "operational_wal": Path(str(path) + "-wal"),
                       "learning": Path(path).with_name("learning_loop.sqlite")}.items():
        try:
            sizes[name] = file.stat().st_size
        except FileNotFoundError:
            sizes[name] = None
    return {"load_1m": os.getloadavg()[0], "cpu_count": os.cpu_count(),
            "memory_total_mib": mem.get("MemTotal"), "memory_available_mib": mem.get("MemAvailable"),
            "disk_free_bytes": disk.free, "disk_total_bytes": disk.total,
            "database_bytes": sizes}


def status(database, backup_file):
    readings = resources(database)
    backup = read_status(backup_file)
    alerts = []
    if readings["disk_free_bytes"] < 1 << 30:
        alerts.append("disk_low")
    if readings["load_1m"] > 2 * (readings["cpu_count"] or 1):
        alerts.append("cpu_load_high")
    if (readings["memory_available_mib"] or 0) < 256:
        alerts.append("memory_pressure")
    if freshness(backup.get("verified_at"), 3900) != "current" or backup.get("state") != "verified":
        alerts.append("local_backup_unhealthy")
    if backup.get("offsite") not in ("verified", "unconfigured"):
        alerts.append("offsite_backup_unhealthy")
    try:
        with db(database) as connection:
            schema = connection.execute("PRAGMA user_version").fetchone()[0]
            if get(connection, "schema") != "hourly-paper-1":
                raise ValueError("invalid database family")
            supervisor = freshness(get(connection, "supervise_heartbeat"), 45)
            research = freshness(get(connection, "research_heartbeat"), 3900)
            sync = freshness(get(connection, "sync_at"), 45)
            progress = json.loads(get(connection, "learning_progress", "{}"))
            uncertain = connection.execute("""SELECT count(*) FROM intents
                WHERE state IN ('reserved','dispatching','submission_unknown','reconciliation_required','unknown')""").fetchone()[0]
            if supervisor != "current" or sync != "current" or get(connection, "snapshot") != "ok":
                alerts.append("broker_reconciliation_unhealthy")
            if research != "current" or progress.get("state") == "recovery_required":
                alerts.append("research_unhealthy")
            if progress.get("missing_batch", 0) or progress.get("features_unavailable", 0):
                alerts.append("learning_inputs_incomplete")
            if uncertain:
                alerts.append("uncertain_orders")
            if connection.execute("SELECT 1 FROM lots WHERE CAST(qty AS REAL)>0 LIMIT 1").fetchone():
                alerts.append("position_protection_unverified")
            database_health = {"state": "readable", "schema": schema, "supervisor": supervisor,
                               "research": research, "reconciliation": sync}
    except (OSError, ValueError, sqlite3.Error, AttributeError):
        alerts.append("database_unavailable")
        database_health = {"state": "unavailable"}
    return {"at": now(), "resources": readings, "database": database_health,
            "backup": backup, "alerts": alerts}


def heartbeat(url, healthy):
    endpoint = urlsplit(url)
    if (endpoint.scheme != "https" or not endpoint.hostname or endpoint.username is not None
            or endpoint.password is not None or endpoint.fragment or endpoint.query):
        raise ValueError("HTTPS heartbeat endpoint required")
    target = url.rstrip("/") + ("" if healthy else "/fail")
    request = Request(target, data=b"", method="POST")
    with build_opener(NoRedirect()).open(request, timeout=5) as response:
        if not 200 <= response.status < 300:
            raise OSError("heartbeat rejected")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    parser.add_argument("backup_status", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    report = status(args.database, args.backup_status)
    destination = os.getenv("PHOENIX_HEARTBEAT_URL")
    report["external_heartbeat"] = "unconfigured"
    if destination:
        try:
            heartbeat(destination, not report["alerts"])
            report["external_heartbeat"] = "accepted"
        except (OSError, ValueError):
            report["external_heartbeat"] = "failed"
    write_status(args.output, report)
    print(json.dumps({"event": "health_sample", "at": report["at"], "alerts": report["alerts"],
                      "external_heartbeat": report["external_heartbeat"]}))
    return int(report["external_heartbeat"] == "failed")


if __name__ == "__main__":
    raise SystemExit(main())
